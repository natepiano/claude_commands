"""Fail shutdown tests before an unfaked send can leave the test process."""

from __future__ import annotations

import faulthandler
import shlex
import socket
import sys
import unittest
from enum import StrEnum
from types import ModuleType
from typing import Never, cast, override
from unittest.mock import patch

import remote
import remote_control
import restart
import herdr
import settle
import shutdown
import sessions
from shutdown_record import SessionShutdown


class ShutdownSendLeaf(StrEnum):
    DELIVER_MESSAGE = "settle._deliver_message"
    RETIRE_SHUTDOWN_MESSAGES = "settle._retire_shutdown_messages"
    RESTART_RETIRE = "restart._retire"
    RESUME_CODEX_WORKERS = "restart._send_worker_resume"
    RUN_REMOTE = "remote.run_remote"
    LAUNCH_CONDUCTOR = "settle._launch_conductor"
    LAUNCH_REMOTE_CONTROL_FIXER = "remote_control.launch_fixer"
    RUN_NOTIFIER = "settle.run_notifier"


# Keep real functions before any test installs guards, including nested proof cases.
_REAL_RESTART_RETIRE = restart._retire  # pyright: ignore[reportPrivateUsage]
_REAL_LEAF_BINDINGS: dict[ShutdownSendLeaf, tuple[tuple[ModuleType, str, object], ...]] = {
    ShutdownSendLeaf.DELIVER_MESSAGE: (
        (settle, "_deliver_message", settle._deliver_message),  # pyright: ignore[reportPrivateUsage]
    ),
    ShutdownSendLeaf.RETIRE_SHUTDOWN_MESSAGES: (
        (settle, "_retire_shutdown_messages", settle._retire_shutdown_messages),  # pyright: ignore[reportPrivateUsage]
    ),
    ShutdownSendLeaf.RESTART_RETIRE: ((restart, "_retire", _REAL_RESTART_RETIRE),),
    ShutdownSendLeaf.RESUME_CODEX_WORKERS: (
        (restart, "_send_worker_resume", restart._send_worker_resume),  # pyright: ignore[reportPrivateUsage]
    ),
    ShutdownSendLeaf.RUN_REMOTE: (
        (remote, "run_remote", remote.run_remote),
        (settle, "run_remote", remote.run_remote),
        (restart, "run_remote", remote.run_remote),
        (shutdown, "run_remote", remote.run_remote),
    ),
    ShutdownSendLeaf.LAUNCH_CONDUCTOR: (
        (settle, "_launch_conductor", settle._launch_conductor),  # pyright: ignore[reportPrivateUsage]
    ),
    ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER: (
        (remote_control, "launch_fixer", remote_control.launch_fixer),
    ),
    ShutdownSendLeaf.RUN_NOTIFIER: ((settle, "run_notifier", settle.run_notifier),),
}


def registry_confirmation(ids: frozenset[str]) -> restart.RegistryConfirmation:
    return restart.RegistryConfirmation(tuple(sessions.SessionRecord(pid=index + 1,
        sessionId=identity, name=identity, messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        for index, identity in enumerate(sorted(ids))))


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
        faulthandler.dump_traceback_later(TEST_TIME_LIMIT_SECONDS, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        # Records and reports in these fixtures belong to natedev.
        _ = self.enterContext(patch.object(socket, "gethostname", return_value="natedev"))
        # Launch limits stay as written on a loaded machine.
        _ = self.enterContext(patch.object(herdr, "load_factor", return_value=1.0))
        self._send_leaf_hits.clear()
        # Run after patch cleanups, so caught assertions still fail the test.
        self.addCleanup(self._fail_send_leaf_hits)
        stubs: dict[ShutdownSendLeaf, object] = {
            ShutdownSendLeaf.DELIVER_MESSAGE: self._block_deliver_message,
            ShutdownSendLeaf.RETIRE_SHUTDOWN_MESSAGES: self._block_retire_shutdown_messages,
            ShutdownSendLeaf.RESTART_RETIRE: self._block_restart_retire,
            ShutdownSendLeaf.RESUME_CODEX_WORKERS: self._block_worker_resume,
            ShutdownSendLeaf.RUN_REMOTE: self._block_run_remote,
            ShutdownSendLeaf.LAUNCH_CONDUCTOR: self._block_launch_conductor,
            ShutdownSendLeaf.RUN_NOTIFIER: self._block_run_notifier,
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

    def _block_retire_shutdown_messages(self, login: str, session_id: str) -> Never:
        del login
        self._reached_send_leaf(
            ShutdownSendLeaf.RETIRE_SHUTDOWN_MESSAGES, f"session:{session_id}"
        )

    def _block_restart_retire(
        self, record: SessionShutdown, *, dry_run: bool
    ) -> None:
        if dry_run:
            _REAL_RESTART_RETIRE(record, dry_run=True)
            return
        self._reached_send_leaf(
            ShutdownSendLeaf.RESTART_RETIRE,
            f"session:{record['session']['session_id']}",
        )

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

    def _block_worker_resume(self, record: SessionShutdown) -> None:
        if record["session"]["kind"] == "unit" and record.get("codex_seats"):
            self._reached_send_leaf(
                ShutdownSendLeaf.RESUME_CODEX_WORKERS,
                f"session:{record['session']['session_id']}",
            )

    def _block_launch_conductor(self, login: str, label: str, here: bool = True) -> Never:
        del label, here
        self._reached_send_leaf(
            ShutdownSendLeaf.LAUNCH_CONDUCTOR, f"conductor for {login}"
        )

    def _block_launch_remote_control(
        self, session_id: str, pid: int, proc_start: str, who: str,
        session: str
    ) -> Never:
        del pid, proc_start, who, session
        self._reached_send_leaf(ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER, f"session:{session_id}")

    def _block_run_notifier(self, verb: str, instance: str) -> Never:
        del verb
        self._reached_send_leaf(ShutdownSendLeaf.RUN_NOTIFIER, f"notifier {instance}")
