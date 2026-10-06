#!/usr/bin/env python3
"""Tests for one reachable address per live top-level session."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import override
from unittest import mock

import top_level


class TopLevelTests(unittest.TestCase):
    folder: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.folder),
            "NOTIFIER_SESSIONS_DIR": str(self.folder),
            "CLAUDE_CODE_SESSION_ID": "current-id",
            "BUILD_HOLD_DIR": str(self.folder / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.folder / "release"),
        }))

    def add(
        self, name: str, *, session_id: str = "", file_name: str = "",
        socket: str | None = "auto", pid: int | None = None,
        start: str | None = None, tmux: str | None = None,
    ) -> str:
        pid = os.getpid() if pid is None else pid
        session: dict[str, object] = {
            "pid": pid,
            "name": name,
            "sessionId": session_id or name,
            "procStart": start if start is not None else top_level.proc_start(pid),
        }
        if socket is not None:
            address = str(self.folder / f"{file_name or name}.sock") if socket == "auto" else socket
            session["messagingSocketPath"] = address
        else:
            address = ""
        if tmux is not None:
            session["tmux"] = tmux
        _ = (self.folder / f"{file_name or name}.json").write_text(json.dumps(session))
        return address

    def run_main(self, *argv: str, record_exit: int = 0) -> tuple[int, list[str], list[str], list[tuple[str, str]]]:
        output = io.StringIO()
        errors = io.StringIO()
        recorded: list[tuple[str, str]] = []

        def record(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
            self.assertIn("record-recipient", command)
            recorded.append((command[command.index("--name") + 1], command[command.index("--session-id") + 1]))
            return subprocess.CompletedProcess(command, record_exit, b"", b"")

        with mock.patch.object(subprocess, "run", side_effect=record), \
                redirect_stdout(output), redirect_stderr(errors):
            result = top_level.main(list(argv))
        return result, output.getvalue().splitlines(), errors.getvalue().splitlines(), recorded

    def test_two_sessions_with_one_name_have_distinct_addresses_and_records(self) -> None:
        first = self.add("shared", session_id="session-a", file_name="first")
        second = self.add("shared", session_id="session-b", file_name="second")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(lines, [f"shared\tuds:{first}", f"shared\tuds:{second}"])
        self.assertEqual(errors, [])
        self.assertEqual(recorded, [("shared", "session-a"), ("shared", "session-b")])

    def test_sorting_uses_name_then_address(self) -> None:
        zulu = self.add("zulu", socket="/tmp/zulu.sock")
        second = self.add("shared", session_id="b", file_name="first", socket="/tmp/b.sock")
        first = self.add("shared", session_id="a", file_name="second", socket="/tmp/a.sock")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(lines, [f"shared\tuds:{first}", f"shared\tuds:{second}", f"zulu\tuds:{zulu}"])
        self.assertEqual(errors, [])
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
        sessions = top_level.forwarded_sessions(self.folder, lambda _target: False)
        self.assertEqual(len(sessions), 2)
        reachable = next(session for session in sessions if session.name == "reachable")
        missing = next(session for session in sessions if session.name == "missing")
        self.assertIsInstance(reachable, top_level.AddressableSession)
        assert isinstance(reachable, top_level.AddressableSession)
        self.assertEqual(reachable.session_id, "reachable-id")
        self.assertEqual(reachable.address, f"uds:{address}")
        self.assertIsInstance(missing, top_level.UnaddressableSession)
        self.assertEqual(missing.session_id, "missing-id")

    def test_own_id_is_left_out_while_another_session_with_its_name_is_sent(self) -> None:
        _ = self.add("shared", session_id="current-id", file_name="current")
        other = self.add("shared", session_id="other-id", file_name="other")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(lines, [f"shared\tuds:{other}"])
        self.assertEqual(errors, [])
        self.assertEqual(recorded, [("shared", "other-id")])

    def test_unset_or_empty_session_id_exits_before_recording(self) -> None:
        _ = self.add("reachable")
        for value in (None, ""):
            with self.subTest(value=value), mock.patch.dict(os.environ):
                if value is None:
                    _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
                else:
                    os.environ["CLAUDE_CODE_SESSION_ID"] = value
                result, lines, errors, recorded = self.run_main()
                self.assertEqual(result, 2)
                self.assertEqual(lines, [])
                self.assertEqual(errors, [
                    "top_level: CLAUDE_CODE_SESSION_ID is unset, so this session cannot be left out",
                ])
                self.assertEqual(recorded, [])

    def test_unit_directors_are_left_out(self) -> None:
        address = self.add("showrunner")
        other = self.add("ordinary", tmux="ordinary:@3.%3")
        _ = self.add("trunk", tmux="unit-trunk:@0.%0")
        sessions = top_level.forwarded_sessions(
            self.folder, lambda target: target.startswith("unit-")
        )
        self.assertEqual(
            [(session.name, session.session_id) for session in sessions],
            [("ordinary", "ordinary"), ("showrunner", "showrunner")],
        )
        self.assertEqual(
            [session.address for session in sessions if isinstance(session, top_level.AddressableSession)],
            [f"uds:{other}", f"uds:{address}"],
        )

    def test_ended_and_reused_pids_are_left_out(self) -> None:
        _ = self.add("ended", pid=2**22 + 1, start="1")
        _ = self.add("reused", start="1")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(lines, [])
        self.assertEqual(errors, [])
        self.assertEqual(recorded, [])

    def test_failed_recipient_record_returns_one(self) -> None:
        _ = self.add("reachable", session_id="reachable-id")
        result, lines, errors, recorded = self.run_main(record_exit=1)
        self.assertEqual(result, 1)
        self.assertEqual(lines, [])
        self.assertEqual(errors, ["could not record recipient reachable [reachable-id]"])
        self.assertEqual(recorded, [("reachable", "reachable-id")])

    def test_self_argument_is_no_longer_accepted(self) -> None:
        _ = self.add("reachable")
        result, lines, _errors, recorded = self.run_main("--self", "current")
        self.assertEqual(result, 2)
        self.assertEqual(lines, [])
        self.assertEqual(recorded, [])


if __name__ == "__main__":
    _ = unittest.main()
