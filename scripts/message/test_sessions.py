"""Live session lookup through the sessions.py command line."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from dataclasses import replace
from collections.abc import Iterator
from typing import cast, override
from unittest import mock

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.transcript_test_support import compressed_copy
from scripts.agents.test_support import ShutdownTestCase
from scripts.message import sessions, session_records
from scripts.message import process_identity
from scripts.message.relay_identity import SavedRelaySender, read_relay_sender, relay_session_name
from scripts.message.relay_test_support import write_relay_record
from scripts.message.recipient_test_support import staff_child_command
from scripts.production import staff


SCRIPT = Path(__file__).with_name("sessions.py")
STAT_WITH_TRUNCATED_PROCESS_NAME = b"4242 (with ) spaces \xc3) " + b" ".join(
    [b"S", *[b"0"] * 18, b"8675309", b"unused"]
)


class SessionLookupTests(ShutdownTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.sessions: Path = Path()
        self.socket_path: Path = Path()
        self.socket: socket.socket = socket.socket(socket.AF_UNIX)
        self.members: dict[str, staff.StaffRow] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **interpreter_cache_environment(self.root / "cache"), "HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
        })))
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.addCleanup(self.socket.close)
        self.socket_path = Path(self.enterContext(short_socket_directory())) / "live.sock"
        self.socket.bind(str(self.socket_path))
        self.members = {}
        _ = self.enterContext(mock.patch.object(staff, "snapshot",
                                               side_effect=lambda: staff.StaffSnapshot(tuple(self.members.values()))))
        _ = self.enterContext(mock.patch.object(time, "sleep"))

    def record(
        self, filename: str, *, pid: int | None = None, name: str = "agent",
        session_id: str = "session-1", socket_path: Path | None = None, updated: int = 1,
        proc_start: str = "", entrypoint: str = "",
    ) -> None:
        data = {
            "pid": os.getpid() if pid is None else pid,
            "name": name,
            "sessionId": session_id,
            "messagingSocketPath": str(self.socket_path if socket_path is None else socket_path),
            "updatedAt": updated,
            "procStart": proc_start,
            **({"entrypoint": entrypoint} if entrypoint else {}),
        }
        _ = (self.sessions / filename).write_text(json.dumps(data))
        if not entrypoint.startswith("sdk-") and not any(row.name == name for row in self.members.values()):
            self.members[session_id] = staff.StaffRow(staff.StaffId(session_id), "unit", name, staff.StaffRoot(),
                                                     current_session=staff.StaffId(session_id))

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            staff_child_command(SCRIPT, list(self.members.values()), *args),
            env={**os.environ, "NOTIFIER_SESSIONS_DIR": str(self.sessions)},
            capture_output=True, text=True, check=False,
        )

    def test_transcript_lookup_keeps_account_paths_and_newest_mtime(self) -> None:
        projects = self.root / ".claude/projects"
        older = projects / "older/saved.jsonl"
        newest = projects / "newest/saved.jsonl"
        other = self.root / ".claude2/projects/other/saved.jsonl"
        for path, modified in ((older, 10), (newest, 30), (other, 20)):
            path.parent.mkdir(parents=True)
            _ = path.write_text('{"type":"assistant"}\n')
            os.utime(path, (modified, modified))
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                if compressed:
                    older, newest, other = (compressed_copy(path) for path in (older, newest, other))
                self.assertEqual(sessions.transcript_locations("saved"), sorted((older, newest, other)))
                self.assertEqual(sessions.latest_transcript("saved", projects), newest)

    def test_session_process_count_includes_every_record_for_the_id(self) -> None:
        self.record("first.json", session_id="attached")
        self.record("second.json", session_id="attached")
        self.record("other.json", session_id="other")
        self.record("dead.json", session_id="attached", pid=2**29 + os.getpid())
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            observed = sessions.session_processes("attached")
            absent = sessions.session_processes("absent")
        self.assertIsInstance(observed, sessions.SessionProcesses)
        assert isinstance(observed, sessions.SessionProcesses)
        self.assertEqual(observed.pids, (os.getpid(), os.getpid()))
        self.assertEqual(absent, sessions.SessionProcesses(()))

    def test_unreadable_record_does_not_change_a_session_answer(self) -> None:
        self.record("live.json", session_id="attached")
        _ = (self.sessions / "broken.json").write_text("{broken")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual(sessions.session_processes("attached"), sessions.SessionProcesses((os.getpid(),)))
            self.assertEqual(sessions.session_liveness("attached"), sessions.LiveSessionProcess(os.getpid()))
            self.assertEqual(sessions.session_liveness("absent"), sessions.SessionNotLive())

    def test_remote_control_reads_present_missing_empty_and_unreadable_fields(self) -> None:
        self.record(f"{os.getpid()}.json")
        path = self.sessions / f"{os.getpid()}.json"
        registered = sessions.read_session(path)
        assert not isinstance(registered, sessions.UnreadableSessionRecord)
        original = path.read_text()
        for value, expected in (("bridge-id", "active"), ("", "absent"), (None, "absent"), (3, "absent")):
            with self.subTest(value=value):
                data = cast(dict[str, object], json.loads(original))
                if value is not None:
                    data["bridgeSessionId"] = value
                _ = path.write_text(json.dumps(data))
                with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
                    self.assertEqual(sessions.remote_control(registered), expected)
        _ = path.write_text("broken JSON")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual(sessions.remote_control(registered), "absent")
        path.unlink()
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual(sessions.remote_control(registered), "absent")

    def test_live_name_session_id_and_pid_lookup(self) -> None:
        self.record("one.json", name="showrunner", session_id="sid-123")
        self.assertEqual(self.run_cli("socket", "showrunner").stdout.strip(), str(self.socket_path))
        self.assertEqual(self.run_cli("socket", "session:sid-123").stdout.strip(), str(self.socket_path))
        # The address is what `SendMessage` takes: this machine's session, by its socket.
        self.assertEqual(self.run_cli("address", "showrunner").stdout, f"uds:{self.socket_path}\n")
        self.assertEqual(self.run_cli("address", "session:sid-123").stdout, f"uds:{self.socket_path}\n")
        self.assertEqual(self.run_cli("id", "showrunner").stdout.strip(), "sid-123")
        self.assertEqual(self.run_cli("id", str(os.getpid())).stdout.strip(), "sid-123")
        data = cast(dict[str, object], json.loads((self.sessions / "one.json").read_text()))
        data["name"] = "registry label"
        data["formerNames"] = ["former label"]
        _ = (self.sessions / "one.json").write_text(json.dumps(data))
        self.members = {"fixed": staff.StaffRow(staff.StaffId("fixed"), "unit", "showrunner", staff.StaffRoot(),
                                               current_session=staff.StaffId("sid-123"))}
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            live = sessions.live_sessions()
        for target, expected in (("showrunner", "sid-123"), ("showrunner-unit", "sid-123"),
                                 ("registry label", ""), ("former label", ""), ("unknown-recipient", ""),
                                 ("session:sid-123", "sid-123"), (f"uds:{self.socket_path}", "sid-123")):
            with self.subTest(target=target):
                found = sessions.addressed(target, live)
                self.assertEqual(found["sessionId"] if found is not None else "", expected)
                for command, value in (("socket", str(self.socket_path)), ("address", f"uds:{self.socket_path}"),
                                       ("id", "sid-123")):
                    result = self.run_cli(command, target)
                    self.assertEqual((result.returncode, result.stdout), (0, value + "\n") if expected else (1, ""))
                    self.assertEqual(result.stderr, "" if expected else f"sessions: no live session for {target!r}\n")

    def test_a_relay_named_like_a_session_is_never_found_or_listed(self) -> None:
        relay_socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(relay_socket.close)
        relay_path = Path(self.enterContext(short_socket_directory())) / "relay.sock"
        relay_socket.bind(str(relay_path))
        self.record("session.json", name="natedev", session_id="sid-session", updated=10, entrypoint="cli")
        # send.py's relay is a newer `claude -p` run started with `-n natedev`.
        self.record("relay.json", name="natedev", session_id="sid-relay", socket_path=relay_path, updated=20,
                    entrypoint="sdk-cli")
        self.assertEqual(self.run_cli("address", "natedev").stdout, f"uds:{self.socket_path}\n")
        self.assertEqual(self.run_cli("socket", "session:sid-relay").returncode, 1)
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            live = sessions.live_sessions()
            running = sessions.running_sessions()
        self.assertEqual([record["sessionId"] for record in live], ["sid-session"])
        self.assertEqual([record["sessionId"] for record in running], ["sid-session"])
        found = sessions.addressed("natedev", live)
        self.assertEqual(found["sessionId"] if found else None, "sid-session")

    def test_a_direct_launch_with_a_relay_form_name_remains_visible(self) -> None:
        name = relay_session_name("natedev")
        self.record("real.json", name=name, session_id="sid-real", entrypoint="cli")
        self.assertEqual(self.run_cli("address", name).stdout, f"uds:{self.socket_path}\n")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual([row["sessionId"] for row in sessions.running_sessions()], ["sid-real"])

    def test_a_registry_rename_keeps_the_staff_name_reachable(self) -> None:
        self.record("real.json", name="natedev", session_id="sid-real", entrypoint="cli")
        path = self.sessions / "real.json"
        record = sessions.read_session(path)
        assert not isinstance(record, sessions.UnreadableSessionRecord)
        record["name"] = relay_session_name("natedev")
        record["formerNames"] = ["natedev"]
        _ = path.write_text(json.dumps(record))
        for name in ("natedev", "session:sid-real"):
            with self.subTest(name=name):
                result = self.run_cli("address", name)
                self.assertEqual((result.returncode, result.stdout), (0, f"uds:{self.socket_path}\n"))
        self.assertEqual(self.run_cli("id", "natedev").stdout, "sid-real\n")
        self.assertEqual(self.run_cli("address", record["name"]).returncode, 1)
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            live = sessions.live_sessions()
        self.assertEqual(sessions.addressed("natedev", live), record)
        fixed = self.members["sid-real"]
        self.record("replacement.json", name="replacement-display", session_id="sid-replacement", entrypoint="cli")
        self.members[fixed.id] = replace(fixed, current_session=staff.StaffId("sid-replacement"))
        replacement = self.run_cli("member-socket", "session:" + fixed.id)
        self.assertEqual((replacement.returncode, replacement.stdout, replacement.stderr),
                         (0, str(self.socket_path) + "\n", ""))

    def test_a_saved_pid_identifies_a_live_relay_without_a_name_or_entrypoint_hint(self) -> None:
        self.record("relay.json", name="ordinary-looking-name", session_id="sid-relay")
        write_relay_record(self.root / "state/message", os.getpid(), "natedev")
        self.assertEqual(self.run_cli("address", "ordinary-looking-name").returncode, 1)
        self.assertEqual(self.run_cli("socket", "session:sid-relay").returncode, 1)
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual(sessions.live_sessions(), [])
            self.assertEqual(sessions.running_sessions(), [])

    def test_a_reused_pid_does_not_hide_a_real_session_or_erase_the_saved_sender(self) -> None:
        name = relay_session_name("natedev")
        self.record("real.json", name=name, session_id="sid-real", entrypoint="cli")
        directory = self.root / "state/message/relay_senders"
        write_relay_record(self.root / "state/message", os.getpid(), "natedev")
        path = directory / f"{os.getpid()}.identity"
        original = cast(dict[str, str], json.loads(path.read_text()))
        for field in ("started", "boot"):
            with self.subTest(field=field):
                stale = {**original, field: "previous-process-lifetime"}
                _ = path.write_text(json.dumps(stale))
                found = self.run_cli("address", name)
                self.assertEqual((found.returncode, found.stdout), (0, f"uds:{self.socket_path}\n"))
                self.assertEqual(read_relay_sender(os.getpid(), directory), SavedRelaySender("natedev"))

    def test_a_legacy_sender_record_cannot_hide_a_real_session(self) -> None:
        self.record("real.json", name="natedev", session_id="sid-real", entrypoint="cli")
        directory = self.root / "state/message/relay_senders"
        write_relay_record(self.root / "state/message", os.getpid(), "former-sender")
        (directory / f"{os.getpid()}.identity").unlink()
        self.assertEqual(self.run_cli("address", "natedev").stdout, f"uds:{self.socket_path}\n")
        self.assertEqual(read_relay_sender(os.getpid(), directory), SavedRelaySender("former-sender"))

    def test_newest_live_match_wins(self) -> None:
        other = socket.socket(socket.AF_UNIX)
        self.addCleanup(other.close)
        other_path = Path(self.enterContext(short_socket_directory())) / "newer.sock"
        other.bind(str(other_path))
        self.record("old.json", name="same", session_id="same-id", updated=10)
        self.record("new.json", name="same", session_id="same-id", socket_path=other_path, updated=20)
        self.assertEqual(self.run_cli("socket", "same").stdout.strip(), str(other_path))
        self.assertEqual(self.run_cli("socket", "session:same-id").stdout.strip(), str(other_path))

    def test_staff_names_take_priority_over_unit_forms(self) -> None:
        def record(name: str, session_id: str) -> sessions.SessionRecord:
            return sessions.SessionRecord(pid=1, sessionId=session_id, name=name, messagingSocketPath="", updatedAt=1,
                                          tmux="", formerNames=[])
        records = [record("handler", "bare"), record("modules-unit", "suffixed"), record("modules", "exact")]
        self.members = {row["sessionId"]: staff.StaffRow(staff.StaffId(row["sessionId"]), "unit", row["name"],
                                                       staff.StaffRoot(), current_session=staff.StaffId(row["sessionId"]))
                        for row in records}
        found = [sessions.named(name, records) for name in ("handler", "handler-unit", "modules-unit", "modules", "gone")]
        self.assertEqual([None if item is None else item["sessionId"] for item in found],
                         ["bare", "bare", "suffixed", "exact", None])

    def test_dead_pid_and_missing_socket_return_absent(self) -> None:
        self.record("dead.json", pid=2**29 + os.getpid())
        self.record("missing-socket.json", socket_path=self.root / "absent.sock")
        for args in (("socket", "agent"), ("address", "agent"), ("id", "agent")):
            result = self.run_cli(*args)
            self.assertEqual(
                (result.returncode, result.stdout, result.stderr), (1, "", "sessions: no live session for 'agent'\n")
            )

    def test_running_records_include_socketless_sessions_but_live_records_do_not(self) -> None:
        started = sessions.process_start(os.getpid())
        assert started is not None
        self.record("reachable.json", session_id="reachable", updated=10)
        self.record("socketless.json", session_id="socketless", updated=20, proc_start=started)
        path = self.sessions / "socketless.json"
        record = sessions.read_session(path)
        assert not isinstance(record, sessions.UnreadableSessionRecord)
        record["messagingSocketPath"] = ""
        self.assertEqual(record.get("procStart"), started)
        _ = path.write_text(json.dumps(record))
        self.record("dead.json", pid=2**29 + os.getpid(), updated=30)
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            running = sessions.running_sessions()
            live = sessions.live_sessions()
        self.assertEqual([record["sessionId"] for record in running], ["socketless", "reachable"])
        self.assertEqual([record["sessionId"] for record in live], ["reachable"])

    def test_session_activity_uses_the_shared_record_reader(self) -> None:
        for state, expected in (("busy", "busy"), ("shell", "shell"), ("idle", "idle"),
                                ("unknown", "unknown"), (None, ""), (42, "")):
            with self.subTest(state=state):
                self.record("activity.json")
                path = self.sessions / "activity.json"
                data = cast(dict[str, object], json.loads(path.read_text()))
                if state is not None:
                    data["status"] = state
                _ = path.write_text(json.dumps(data))
                result = sessions.read_session(path)
                assert not isinstance(result, sessions.UnreadableSessionRecord)
                self.assertEqual(result.get("status"), expected)

    def test_live_sessions_selects_records_through_the_live_session_predicate(self) -> None:
        self.record("fixture.json", pid=2**29 + os.getpid(), socket_path=self.root / "absent.sock")
        record = sessions.read_session(self.sessions / "fixture.json")
        assert not isinstance(record, sessions.UnreadableSessionRecord)
        self.assertFalse(sessions.running_session(record))
        self.assertFalse(sessions.messaging_socket_exists(record))
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}), \
                mock.patch.object(session_records, "live_session", return_value=True) as accepts:
            live = sessions.live_sessions()
        self.assertEqual(live, [record])
        accepts.assert_called_once_with(record)

    def test_record_with_socket_and_wrong_process_start_is_not_running(self) -> None:
        self.record("reused.json", pid=4242, proc_start="recorded start")
        registered = sessions.read_session(self.sessions / "reused.json")
        assert not isinstance(registered, sessions.UnreadableSessionRecord)
        self.assertTrue(sessions.messaging_socket_exists(registered))
        with (
            mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}),
            mock.patch.object(os, "kill"),
            mock.patch.object(session_records, "process_start", return_value="new process start") as start,
        ):
            self.assertFalse(sessions.running_session(registered))
            self.assertFalse(sessions.live_session(registered))
            self.assertEqual(sessions.running_sessions(), [])
            self.assertEqual(sessions.live_sessions(), [])
        self.assertTrue(start.called)
        for call in start.call_args_list:
            self.assertEqual(call.args, (4242,))

    def test_record_with_socket_and_matching_process_start_is_live(self) -> None:
        self.record("matching.json", pid=4242, proc_start="recorded start")
        registered = sessions.read_session(self.sessions / "matching.json")
        assert not isinstance(registered, sessions.UnreadableSessionRecord)
        with (
            mock.patch.object(os, "kill"),
            mock.patch.object(session_records, "process_start", return_value="recorded start"),
        ):
            self.assertTrue(sessions.running_session(registered))
            self.assertTrue(sessions.live_session(registered))

    def test_record_with_socket_and_unreadable_process_start_is_not_running(self) -> None:
        self.record("unknown.json", pid=4242, proc_start="recorded start")
        registered = sessions.read_session(self.sessions / "unknown.json")
        assert not isinstance(registered, sessions.UnreadableSessionRecord)
        with (
            mock.patch.object(os, "kill"),
            mock.patch.object(session_records, "process_start", return_value=None),
        ):
            self.assertFalse(sessions.running_session(registered))
            self.assertFalse(sessions.live_session(registered))

    def test_socketless_record_with_another_process_start_is_not_running(self) -> None:
        self.record("reused.json", socket_path=self.root / "absent.sock", proc_start="another start")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual(sessions.running_sessions(), [])
            self.assertEqual(sessions.live_sessions(), [])

    def test_socketless_records_with_a_truncated_process_name_require_a_matching_start(self) -> None:
        self.record(
            "matching.json", session_id="matching", socket_path=self.root / "absent.sock",
            proc_start="8675309",
        )
        self.record(
            "different.json", session_id="different", socket_path=self.root / "absent.sock",
            proc_start="another start",
        )
        stat_path = self.root / "stat"
        _ = stat_path.write_bytes(STAT_WITH_TRUNCATED_PROCESS_NAME)

        def path_for_stat(value: str) -> Path:
            return stat_path if value == f"/proc/{os.getpid()}/stat" else Path(value)

        with (
            mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}),
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(process_identity, "Path", wraps=Path, side_effect=path_for_stat),
        ):
            for reader, expected in (
                (sessions.running_sessions, ["matching"]), (sessions.live_sessions, []),
            ):
                with self.subTest(reader=reader.__name__):
                    self.assertEqual([record["sessionId"] for record in reader()], expected)

    def test_socketless_record_without_a_process_start_is_not_running(self) -> None:
        self.record("missing-start.json", socket_path=self.root / "absent.sock")
        path = self.sessions / "missing-start.json"
        data = cast(dict[str, object], json.loads(path.read_text()))
        del data["procStart"]
        _ = path.write_text(json.dumps(data))
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual(sessions.running_sessions(), [])

    def test_socketless_record_with_an_empty_process_start_is_not_running(self) -> None:
        self.record("empty-start.json", socket_path=self.root / "absent.sock")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            self.assertEqual(sessions.running_sessions(), [])

    def test_record_with_socket_and_no_process_start_stays_live_without_reading_the_start(self) -> None:
        self.record("legacy.json")
        path = self.sessions / "legacy.json"
        data = cast(dict[str, object], json.loads(path.read_text()))
        del data["procStart"]
        _ = path.write_text(json.dumps(data))
        record = sessions.read_session(path)
        assert not isinstance(record, sessions.UnreadableSessionRecord)
        self.assertEqual(record.get("procStart"), "")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}), \
                mock.patch.object(session_records, "process_start", side_effect=AssertionError("unexpected start read")):
            self.assertEqual(sessions.live_sessions(), [record])

    def test_empty_readable_registry_returns_absent(self) -> None:
        result = self.run_cli("socket", "session:missing")
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (1, "", "sessions: no live session for 'session:missing'\n")
        )

    def test_missing_registry_returns_unknown(self) -> None:
        self.sessions.rmdir()
        result = self.run_cli("socket", "session:missing")
        self.assertEqual((result.returncode, result.stdout), (3, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_registry_that_cannot_be_listed_returns_unknown(self) -> None:
        self.sessions.chmod(0)
        self.addCleanup(self.sessions.chmod, 0o700)
        try:
            _ = list(self.sessions.iterdir())
        except PermissionError:
            pass
        else:
            self.skipTest("current user can list a mode-zero directory")
        result = self.run_cli("socket", "session:missing")
        self.assertEqual((result.returncode, result.stdout), (3, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_corrupt_registry_record_returns_unknown(self) -> None:
        _ = (self.sessions / "broken.json").write_text("{")
        result = self.run_cli("socket", "session:session-1")
        self.assertEqual((result.returncode, result.stdout), (3, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_oversized_pid_does_not_hide_live_matching_session(self) -> None:
        self.record("oversized.json", pid=10**100, name="agent", updated=20)
        self.record("live.json", name="agent", updated=10)
        result = self.run_cli("socket", "agent")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.socket_path)))

    def test_usage_errors(self) -> None:
        for args in ((), ("socket",), ("address",), ("id",), ("unknown", "agent")):
            self.assertEqual(self.run_cli(*args).returncode, 2)

    def test_registry_read_reports_directory_failure_instead_of_empty_success(self) -> None:
        original = Path.iterdir

        def paths(path: Path) -> Iterator[Path]:
            if path == self.sessions:
                raise PermissionError("registry denied")
            return original(path)

        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}), mock.patch.object(Path, "iterdir", paths):
            read = sessions.read_registry()
            self.assertEqual(read.unreadable, (self.sessions,))
            self.assertEqual(read.entries, ())
            self.assertEqual(sessions.live_sessions(), [])

    def test_registry_file_removed_after_listing_is_absent_not_unreadable(self) -> None:
        self.record("vanished.json")
        original = Path.read_text
        def read(path: Path) -> str:
            if path == self.sessions / "vanished.json":
                path.unlink(missing_ok=True)
                raise FileNotFoundError(path)
            return original(path)
        with (mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}),
              mock.patch.object(Path, "read_text", read)):
            registry = sessions.read_registry()
            self.assertEqual(registry, sessions.RegistryRead((), ()))
            self.record("vanished.json")
            self.assertEqual(sessions.session_liveness("absent"), sessions.SessionNotLive())
            self.record("vanished.json")
            self.assertEqual(sessions.main(["sessions.py", "id", "absent"]), 1)

    def test_registry_rewrite_is_reread_by_the_shared_reader(self) -> None:
        self.record("rewriting.json")
        path = self.sessions / "rewriting.json"
        valid = path.read_text()
        for direct in (True, False):
            with self.subTest(direct=direct), mock.patch.object(Path, "read_text", side_effect=["{", valid]) as read:
                with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
                    if direct:
                        result = sessions.read_session(path)
                    else:
                        registry = sessions.read_registry()
                        self.assertEqual(len(registry.entries), 1)
                        result = registry.entries[0].record
                self.assertNotIsInstance(result, sessions.UnreadableSessionRecord)
                self.assertEqual(read.call_count, 2)

    def test_persistently_unreadable_registry_read_uses_the_configured_rereads(self) -> None:
        policy = sessions.retry_policy()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")) as read, \
             mock.patch.object(time, "sleep") as sleep:
            self.assertEqual(sessions.read_session(self.sessions / "bad.json"), sessions.UnreadableSessionRecord.FOUND)
        self.assertEqual(read.call_count, policy.registry_reads)
        self.assertEqual(sleep.call_args_list, [mock.call(policy.registry_delay_seconds)] * (policy.registry_reads - 1))

    def test_unreadable_dead_pid_is_dropped_but_live_pid_stays_unknown(self) -> None:
        path = self.sessions / "4242.json"
        _ = path.write_text("{")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            for error, expected in ((ProcessLookupError(), ()), (PermissionError(), (path,)), (None, (path,))):
                with self.subTest(error=error), mock.patch.object(os, "kill", side_effect=error):
                    self.assertEqual(sessions.read_registry().unreadable, expected)
                    self.assertEqual(sessions.session_liveness("stopped"), sessions.SessionNotLive())

    def test_record_removed_between_unreadable_read_and_reread_is_dropped(self) -> None:
        path = self.sessions / "4242.json"
        _ = path.write_text("{")
        with (mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}),
              mock.patch.object(Path, "read_text", side_effect=["{", FileNotFoundError(path)])):
            self.assertEqual(sessions.read_registry(), sessions.RegistryRead((), ()))

    def test_registry_read_keeps_good_records_and_names_each_bad_record(self) -> None:
        self.record("good.json", name="kept")
        good = self.sessions / "good.json"
        contents = cast(dict[str, object], json.loads(good.read_text()))
        contents["cwd"], contents["status"] = "/kept-folder", "idle"
        _ = good.write_text(json.dumps(contents))
        broken = self.sessions / "bad.json"
        _ = broken.write_text("{broken")
        with mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.sessions)}):
            read = sessions.read_registry()
            self.assertEqual([item.record["name"] for item in read.entries], ["kept"])
            self.assertEqual(read.unreadable, (broken,))
            self.assertEqual(read.entries[0].details.get("cwd"), "/kept-folder")
            self.assertEqual(read.entries[0].details.get("status"), "idle")
            self.assertEqual([item["name"] for item in sessions.live_sessions()], ["kept"])


class ProcessStartTests(ShutdownTestCase):
    def test_linux_start_survives_a_process_name_cut_inside_a_utf8_character(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stat_path = root / "stat"
        _ = stat_path.write_bytes(STAT_WITH_TRUNCATED_PROCESS_NAME)
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(process_identity, "Path", return_value=stat_path) as path,
        ):
            self.assertEqual(sessions.process_start(4242), "8675309")
        path.assert_called_once_with("/proc/4242/stat")

    def test_linux_unreadable_or_malformed_stat_returns_an_unknown_start(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stat_path = root / "stat"
        stat_bytes = STAT_WITH_TRUNCATED_PROCESS_NAME.replace(b"\xc3", b"x")
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(process_identity, "Path", return_value=stat_path),
        ):
            for contents in (
                b"", b"4242 (name) S", stat_bytes.replace(b")", b""),
                stat_bytes.replace(b"8675309", b"\xff"),
                stat_bytes.replace(b"8675309", b"not-digits"),
            ):
                with self.subTest(contents=contents):
                    _ = stat_path.write_bytes(contents)
                    self.assertIsNone(sessions.process_start(4242))
            stat_path.unlink()
            self.assertIsNone(sessions.process_start(4242))

    def test_darwin_start_is_read_in_the_locale_and_zone_claude_records(self) -> None:
        completed = subprocess.CompletedProcess(
            args=(), returncode=0, stdout="Fri Oct  9 23:24:25 2026\n"
        )
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(subprocess, "run", return_value=completed) as run,
        ):
            started = sessions.process_start(4242)

        self.assertEqual(started, "Fri Oct  9 23:24:25 2026")
        self.assertEqual(run.call_args.args[0], ("ps", "-o", "lstart=", "-p", "4242"))
        environment = cast(dict[str, str], run.call_args.kwargs["env"])
        self.assertEqual((environment["LC_ALL"], environment["TZ"]), ("C", "UTC"))

    def test_linux_start_uses_field_twenty_two_after_the_process_name(self) -> None:
        fields = [b"S", *[b"0"] * 18, b"8675309", b"unused"]
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(Path, "read_bytes", return_value=b"4242 (a process (with) spaces) " + b" ".join(fields)) as read,
        ):
            self.assertEqual(sessions.process_start(4242), "8675309")
        read.assert_called_once_with()


if __name__ == "__main__":
    _ = unittest.main()


