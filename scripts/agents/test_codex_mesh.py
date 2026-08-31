"""Tests for codex_mesh's recovery from an app-server that has wedged.

The failure these cover is silent by construction: a stuck app-server replays a
provider message it cached earlier to every thread that attaches, so a local
fault arrives wearing the exact words of a usage limit. What the code can check
is not the message but the circumstances -- who started the server, how fast it
failed, and whether the delegate had already written anything.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

from scripts.agents import codex_mesh

USAGE_LIMIT = "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage"


def _attempt(seconds: float = 3.0, produced_work: bool = False) -> codex_mesh.Attempt:
    return codex_mesh.Attempt(
        failure=USAGE_LIMIT, produced_work=produced_work, seconds=seconds
    )


class RetryDecisionTests(unittest.TestCase):
    """Which failures earn a second run against a server this process started."""

    def test_a_fast_failure_on_an_inherited_server_is_retried(self) -> None:
        # The incident this exists for: three seats attach to a server left by
        # an earlier dispatch and all three die in seconds with identical text.
        self.assertTrue(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(), fresh_server=False, resident=False
            )
        )

    def test_a_failure_on_a_server_this_call_started_is_final(self) -> None:
        # Nothing was inherited, so the refusal came from the provider and a
        # retry would only ask the same question of the same fresh process.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(), fresh_server=True, resident=False
            )
        )

    def test_a_delegate_that_produced_work_is_never_retried(self) -> None:
        # The interlock: repeating the prompt would apply its edits twice.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(produced_work=True), fresh_server=False, resident=False
            )
        )

    def test_a_slow_failure_reached_the_provider(self) -> None:
        # A cached answer comes back instantly; one that travelled does not.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(seconds=codex_mesh.RETRY_FAST_FAILURE_SECS + 1),
                fresh_server=False,
                resident=False,
            )
        )

    def test_a_resident_delegate_is_never_retried(self) -> None:
        # Its caller is already holding replies; a silent second attempt would
        # arrive behind them.
        self.assertFalse(
            codex_mesh._retry_warranted(  # pyright: ignore[reportPrivateUsage]
                _attempt(), fresh_server=False, resident=True
            )
        )


class ServerRecordTests(unittest.TestCase):
    """Dropping a wedged server, and reaping what was dropped."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    spawned: list[subprocess.Popen[bytes]]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temporary.name)
        self.spawned = []

    @override
    def tearDown(self) -> None:
        for process in self.spawned:
            with contextlib.suppress(OSError):
                process.kill()
            _ = process.wait(timeout=5)
        self.temporary.cleanup()

    def sleeper(self) -> int:
        """A live pid to stand in for an app-server, with no codex installed."""
        process = subprocess.Popen(["sleep", "30"])
        self.spawned.append(process)
        return process.pid

    def write_server(self, port: int, pid: int) -> None:
        _ = (self.session_dir / codex_mesh.SERVER_FILE).write_text(
            json.dumps({"port": port, "pid": pid}), encoding="utf-8"
        )

    def retired_ports(self) -> list[object]:
        path = self.session_dir / codex_mesh.RETIRED_FILE
        if not path.exists():
            return []
        stored = codex_mesh._as_dict(  # pyright: ignore[reportPrivateUsage]
            json.loads(path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        )
        servers = stored.get("servers")
        if not isinstance(servers, list):
            return []
        return [
            codex_mesh._as_dict(record).get("port")  # pyright: ignore[reportPrivateUsage]
            for record in cast("list[object]", servers)
        ]

    def test_a_live_record_is_attached_to_rather_than_replaced(self) -> None:
        pid = self.sleeper()
        self.write_server(4321, pid)
        port, fresh = codex_mesh.ensure_server(str(self.session_dir))
        # Never started a server, so `codex` need not even be installed here --
        # which is the point: an inherited server is exactly what goes unchecked.
        self.assertEqual((port, fresh), (4321, False))

    def test_retiring_drops_the_record_and_keeps_the_pid_for_stop(self) -> None:
        pid = self.sleeper()
        self.write_server(4321, pid)
        retired = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        self.assertTrue(retired)
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())
        self.assertEqual(self.retired_ports(), [4321])

    def test_a_peer_that_already_replaced_the_server_is_left_alone(self) -> None:
        # Three seats fail together and all three try this recovery. Only the
        # first drops a server; the others would otherwise drop the replacement
        # and restart the cycle they just ended.
        self.write_server(4321, self.sleeper())
        _ = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        self.write_server(9876, self.sleeper())
        retired = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        self.assertFalse(retired)
        record = codex_mesh._as_dict(  # pyright: ignore[reportPrivateUsage]
            json.loads(  # pyright: ignore[reportAny]
                (self.session_dir / codex_mesh.SERVER_FILE).read_text(encoding="utf-8")
            )
        )
        self.assertEqual(record.get("port"), 9876)
        self.assertEqual(self.retired_ports(), [4321])

    def test_stop_reaps_the_retired_server_as_well_as_the_live_one(self) -> None:
        # A retired server is abandoned rather than signalled while the run is
        # going, so the end of the run is the only place that can free it.
        stale_pid = self.sleeper()
        self.write_server(4321, stale_pid)
        _ = codex_mesh._retire_server(  # pyright: ignore[reportPrivateUsage]
            str(self.session_dir), 4321
        )
        live_pid = self.sleeper()
        self.write_server(9876, live_pid)

        code = codex_mesh.command_stop(
            argparse.Namespace(session_dir=str(self.session_dir))
        )
        self.assertEqual(code, 0)
        # Reap before asserting: these stand-ins are children of the test
        # process, so an unwaited one leaves a pid `os.kill(pid, 0)` accepts.
        # A real app-server is nobody's child and leaves no such entry.
        self.assertEqual([stale_pid, live_pid], [each.pid for each in self.spawned])
        for process in self.spawned:
            self.assertIsNotNone(
                process.wait(timeout=5), f"app-server {process.pid} outlived stop"
            )
        self.assertFalse((self.session_dir / codex_mesh.RETIRED_FILE).exists())
        self.assertFalse((self.session_dir / codex_mesh.SERVER_FILE).exists())


if __name__ == "__main__":
    _ = unittest.main()
