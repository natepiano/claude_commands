"""Tests for the cross-machine shutdown command transport."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest.mock import patch

import remote


class SshInvocation(TypedDict):
    arguments: list[str]
    stdin: str


@final
class RemoteTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.bin = Path()
        self.log = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "ssh.json"
        ssh = self.bin / "ssh"
        _ = ssh.write_text(
            f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys
import time

Path(os.environ["SHUTDOWN_TEST_SSH_LOG"]).write_text(json.dumps({{
    "arguments": sys.argv[1:],
    "stdin": sys.stdin.read(),
}}))
time.sleep(float(os.environ.get("SHUTDOWN_TEST_DELAY", "0")))
print(os.environ.get("SHUTDOWN_TEST_OUTPUT", "remote output"))
status = os.environ.get("SHUTDOWN_TEST_REMOTE_STATUS")
if status is not None:
    print("rc=" + status)
raise SystemExit(int(os.environ.get("SHUTDOWN_TEST_SSH_STATUS", "0")))
""",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "SHUTDOWN_TEST_SSH_LOG": str(self.log),
        }
        patch_dict_context: object = cast(
            object,
            self.enterContext(patch.dict(os.environ, environment, clear=True)),
        )
        del patch_dict_context

    def invocation(self) -> SshInvocation:
        decoded = cast(object, json.loads(self.log.read_text(encoding="utf-8")))
        assert isinstance(decoded, dict)
        values = cast(dict[str, object], decoded)
        arguments = values.get("arguments")
        stdin = values.get("stdin")
        assert isinstance(arguments, list)
        argument_values = cast(list[object], arguments)
        assert all(isinstance(item, str) for item in argument_values)
        assert isinstance(stdin, str)
        return SshInvocation(arguments=cast(list[str], argument_values), stdin=stdin)

    def test_other_machine_maps_linux_and_darwin(self) -> None:
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(remote.other_machine(), "mac")
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(remote.other_machine(), "natedev")

    def test_remote_status_line_controls_return_code(self) -> None:
        os.environ["SHUTDOWN_TEST_REMOTE_STATUS"] = "3"

        status, output = remote.run_remote(
            ["status", "claude 2", "--json"], stdin="request body"
        )

        self.assertEqual((status, output), (3, "remote output"))
        called = self.invocation()
        self.assertEqual(
            called["arguments"][:5],
            ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mac"],
        )
        self.assertEqual(called["stdin"], "request body")
        self.assertNotIn("ServerAliveInterval=15", called["arguments"])
        self.assertNotIn("ServerAliveCountMax=4", called["arguments"])
        command = called["arguments"][5]
        self.assertIn('"$HOME/.claude/scripts/lib/py"', command)
        self.assertIn('"$HOME/.claude/scripts/shutdown/shutdown.py"', command)
        self.assertIn("'claude 2'", command)
        self.assertTrue(command.endswith('; printf "rc=%s\\n" $?'))

    def test_missing_remote_status_line_is_unreachable(self) -> None:
        _ = os.environ.pop("SHUTDOWN_TEST_REMOTE_STATUS", None)
        os.environ["SHUTDOWN_TEST_OUTPUT"] = "partial output"

        status, output = remote.run_remote(["status", "owner@example.com", "--json"])

        self.assertEqual(status, 255)
        self.assertEqual(output, "partial output")

    def test_while_link_alive_adds_keepalives_before_the_host(self) -> None:
        os.environ["SHUTDOWN_TEST_REMOTE_STATUS"] = "0"

        status, _ = remote.run_remote(
            ["stop", "owner@example.com"],
            limit=remote.WhileLinkAlive(kind="while link alive"),
        )

        self.assertEqual(status, 0)
        called = self.invocation()
        self.assertEqual(
            called["arguments"][:9],
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
                "mac",
            ],
        )

    def test_while_link_alive_waits_for_remote_status_beyond_a_time_limit(
        self,
    ) -> None:
        os.environ["SHUTDOWN_TEST_DELAY"] = "2"
        os.environ["SHUTDOWN_TEST_REMOTE_STATUS"] = "7"

        linked_status, _ = remote.run_remote(
            ["stop", "owner@example.com"],
            limit=remote.WhileLinkAlive(kind="while link alive"),
        )
        limited_status, _ = remote.run_remote(
            ["stop", "owner@example.com"],
            limit=remote.TimeLimit(kind="time limit", seconds=1),
        )

        self.assertEqual(linked_status, 7)
        self.assertEqual(limited_status, 255)


if __name__ == "__main__":
    _ = unittest.main()
