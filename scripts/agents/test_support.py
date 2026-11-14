"""Fail shutdown tests before an unfaked send can leave the test process."""

from __future__ import annotations

import faulthandler
import io
import os
import tempfile
import shlex
import shutil
import socket
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from enum import StrEnum
from types import ModuleType
from pathlib import Path
from typing import Never, cast, override
from unittest.mock import patch

from scripts.agents import remote
from scripts.agents import remote_control
from scripts.agents import restart
from scripts.herdr import herdr
from scripts.agents import restart_support
from scripts.agents import shutdown
from scripts.message import sessions
from scripts.agents.unit_staff_test_support import isolated_parents
from scripts.production.staff_test_support import isolated_staff_file, showrunner_relationship


def write_machine_config(directory: Path) -> None:
    """Copy the repository machine table into a private configuration folder."""
    directory.mkdir(parents=True, exist_ok=True)
    _ = shutil.copyfile(Path(__file__).resolve().parents[2] / "config/machines.conf", directory / "machines.conf")


class ShutdownSendLeaf(StrEnum):
    DELIVER_MESSAGE = "restart_support._deliver_message"
    RUN_REMOTE = "remote.run_remote"
    LAUNCH_REMOTE_CONTROL_FIXER = "remote_control.launch_fixer"


_REAL_LEAF_BINDINGS: dict[ShutdownSendLeaf, tuple[tuple[ModuleType, str, object], ...]] = {
    ShutdownSendLeaf.DELIVER_MESSAGE: (
        (restart_support, "_deliver_message", restart_support._deliver_message),  # pyright: ignore[reportPrivateUsage]
    ),
    ShutdownSendLeaf.RUN_REMOTE: ((remote, "run_remote", remote.run_remote),
                                  (shutdown, "run_remote", shutdown.run_remote)),
    ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER: ((remote_control, "launch_fixer", remote_control.launch_fixer),),
}


def registry_confirmation(ids: frozenset[str]) -> restart.RegistryConfirmation:
    return restart.RegistryConfirmation(tuple(sessions.SessionRecord(pid=index + 1,
        sessionId=identity, name=identity, messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        for index, identity in enumerate(sorted(ids))))


def empty_session_registry(test: unittest.TestCase, directory: Path) -> None:
    """Supply complete empty registry evidence alongside a private process fixture."""
    directory.mkdir(parents=True, exist_ok=True)
    _ = cast(object, test.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(directory)})))


def linux_process_fixture(test: unittest.TestCase) -> None:
    """Select the platform modeled by proc trees and systemd stand-ins."""
    _ = cast(object, test.enterContext(patch.object(sys, "platform", "linux")))
    _ = cast(object, test.enterContext(patch.object(herdr, "_PLATFORM", "linux")))


# One test past this prints every thread's stack and ends the run. A conductor loop that never
# finishes (a fake that always fails, with its waits patched away) would otherwise grow without limit.
TEST_TIME_LIMIT_SECONDS = 60


class ShutdownTestCase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._send_leaf_hits: list[str] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        _ = self.enterContext(isolated_parents())
        faulthandler.dump_traceback_later(TEST_TIME_LIMIT_SECONDS, exit=True, file=2)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        registry = Path(self.enterContext(tempfile.TemporaryDirectory())) / "sessions"
        _ = self.enterContext(isolated_staff_file(registry.parent / "staff.json", {}))
        # Seed the local owner record before subclasses select their modeled platform.
        with patch.object(sys, "platform", "linux"):
            _ = showrunner_relationship("fixture")
        empty_session_registry(self, registry)
        # Records and reports in these fixtures belong to natedev.
        _ = self.enterContext(patch.object(socket, "gethostname", return_value="natedev"))
        # Launch limits stay as written on a loaded machine.
        self._send_leaf_hits.clear()
        # Run after patch cleanups, so caught assertions still fail the test.
        self.addCleanup(self._fail_send_leaf_hits)
        stubs: dict[ShutdownSendLeaf, object] = {
            ShutdownSendLeaf.DELIVER_MESSAGE: self._block_deliver_message,
            ShutdownSendLeaf.RUN_REMOTE: self._block_run_remote,
            ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER: self._block_launch_remote_control,
        }
        for leaf, bindings in _REAL_LEAF_BINDINGS.items():
            for module, name, _real in bindings:
                _ = self.enterContext(patch.object(module, name, stubs[leaf]))

    def allow_leaf(self, leaf: ShutdownSendLeaf) -> None:
        """Restore a leaf whose subprocess the test deliberately fakes."""
        if not isinstance(cast(object, leaf), ShutdownSendLeaf):
            raise TypeError("allow_leaf requires a ShutdownSendLeaf")
        for module, name, real in _REAL_LEAF_BINDINGS[leaf]:
            setattr(module, name, real)

    def _reached_send_leaf(self, leaf: ShutdownSendLeaf, recipient: str) -> Never:
        message = (
            f"shutdown test reached real send leaf {leaf.value} for {recipient}; "
            "fake the step above it, or "
            f"allow_leaf(ShutdownSendLeaf.{leaf.name}) and fake the subprocess beneath it"
        )
        self._send_leaf_hits.append(message)
        raise AssertionError(message)

    def _fail_send_leaf_hits(self) -> None:
        if self._send_leaf_hits:
            self.fail("\n".join(self._send_leaf_hits))

    def _block_deliver_message(
        self, command: list[str], recipient: str, text: str
    ) -> Never:
        del command, text
        self._reached_send_leaf(ShutdownSendLeaf.DELIVER_MESSAGE, recipient)


    def _block_run_remote(
        self,
        args: list[str],
        stdin: str = "",
        limit: remote.RemoteCallLimit = remote.STANDARD_TIME_LIMIT,
    ) -> Never:
        del stdin, limit
        self._reached_send_leaf(
            ShutdownSendLeaf.RUN_REMOTE,
            f"{remote.other_machine()}: shutdown.py {shlex.join(args)}",
        )


    def _block_launch_remote_control(
        self, session_id: str, pid: int, proc_start: str, who: str,
        session: str
    ) -> Never:
        del pid, proc_start, who, session
        self._reached_send_leaf(ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER, f"session:{session_id}")
