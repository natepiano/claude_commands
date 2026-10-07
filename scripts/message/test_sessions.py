"""Live session lookup through the sessions.py command line."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override


SCRIPT = Path(__file__).with_name("sessions.py")
PY = Path(__file__).parents[1] / "lib" / "py"


class SessionLookupTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.sessions: Path = Path()
        self.socket_path: Path = Path()
        self.socket: socket.socket = socket.socket(socket.AF_UNIX)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.addCleanup(self.socket.close)
        self.socket_path = self.root / "live.sock"
        self.socket.bind(str(self.socket_path))

    def record(
        self, filename: str, *, pid: int | None = None, name: str = "agent",
        session_id: str = "session-1", socket_path: Path | None = None, updated: int = 1,
    ) -> None:
        data = {
            "pid": os.getpid() if pid is None else pid,
            "name": name,
            "sessionId": session_id,
            "messagingSocketPath": str(self.socket_path if socket_path is None else socket_path),
            "updatedAt": updated,
        }
        _ = (self.sessions / filename).write_text(json.dumps(data))

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PY), str(SCRIPT), *args],
            env={**os.environ, "NOTIFIER_SESSIONS_DIR": str(self.sessions)},
            capture_output=True, text=True, check=False,
        )

    def test_live_name_session_id_and_pid_lookup(self) -> None:
        self.record("one.json", name="showrunner", session_id="sid-123")
        self.assertEqual(self.run_cli("socket", "showrunner").stdout.strip(), str(self.socket_path))
        self.assertEqual(self.run_cli("socket", "session:sid-123").stdout.strip(), str(self.socket_path))
        self.assertEqual(self.run_cli("id", "showrunner").stdout.strip(), "sid-123")
        self.assertEqual(self.run_cli("id", str(os.getpid())).stdout.strip(), "sid-123")

    def test_newest_live_match_wins(self) -> None:
        other = socket.socket(socket.AF_UNIX)
        self.addCleanup(other.close)
        other_path = self.root / "newer.sock"
        other.bind(str(other_path))
        self.record("old.json", name="same", session_id="same-id", updated=10)
        self.record("new.json", name="same", session_id="same-id", socket_path=other_path, updated=20)
        self.assertEqual(self.run_cli("socket", "same").stdout.strip(), str(other_path))
        self.assertEqual(self.run_cli("socket", "session:same-id").stdout.strip(), str(other_path))

    def test_dead_pid_and_missing_socket_return_absent(self) -> None:
        self.record("dead.json", pid=2**29 + os.getpid())
        self.record("missing-socket.json", socket_path=self.root / "absent.sock")
        for args in (("socket", "agent"), ("id", "agent")):
            result = self.run_cli(*args)
            self.assertEqual(
                (result.returncode, result.stdout, result.stderr), (1, "", "")
            )

    def test_empty_readable_registry_returns_absent(self) -> None:
        result = self.run_cli("socket", "session:missing")
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (1, "", "")
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
        for args in ((), ("socket",), ("id",), ("unknown", "agent")):
            self.assertEqual(self.run_cli(*args).returncode, 2)


if __name__ == "__main__":
    _ = unittest.main()
