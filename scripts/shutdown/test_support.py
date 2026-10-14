"""Fail shutdown tests before an unfaked send can leave the test process."""

from __future__ import annotations

import shlex
import unittest
from enum import StrEnum
from types import ModuleType
from typing import Never, cast, override
from unittest.mock import patch

import remote
import restart
import settle
import shutdown
import sessions
from record import SessionShutdown


class ShutdownSendLeaf(StrEnum):
    DELIVER_MESSAGE = "settle._deliver_message"
    RETIRE_SHUTDOWN_MESSAGES = "settle._retire_shutdown_messages"
    RESTART_RETIRE = "restart._retire"
    RUN_REMOTE = "remote.run_remote"
    LAUNCH_CONDUCTOR = "settle._launch_conductor"
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
    ShutdownSendLeaf.RUN_REMOTE: (
        (remote, "run_remote", remote.run_remote),
        (settle, "run_remote", remote.run_remote),
        (restart, "run_remote", remote.run_remote),
        (shutdown, "run_remote", remote.run_remote),
    ),
    ShutdownSendLeaf.LAUNCH_CONDUCTOR: (
        (settle, "_launch_conductor", settle._launch_conductor),  # pyright: ignore[reportPrivateUsage]
    ),
    ShutdownSendLeaf.RUN_NOTIFIER: ((settle, "run_notifier", settle.run_notifier),),
}


def registry_confirmation(ids: frozenset[str]) -> restart.RegistryConfirmation:
    return restart.RegistryConfirmation(tuple(sessions.SessionRecord(pid=index + 1,
        sessionId=identity, name=identity, messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        for index, identity in enumerate(sorted(ids))))


class ShutdownTestCase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._send_leaf_hits: list[str] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self._send_leaf_hits.clear()
        # Run after patch cleanups, so caught assertions still fail the test.
        self.addCleanup(self._fail_send_leaf_hits)
        stubs: dict[ShutdownSendLeaf, object] = {
            ShutdownSendLeaf.DELIVER_MESSAGE: self._block_deliver_message,
            ShutdownSendLeaf.RETIRE_SHUTDOWN_MESSAGES: self._block_retire_shutdown_messages,
            ShutdownSendLeaf.RESTART_RETIRE: self._block_restart_retire,
            ShutdownSendLeaf.RUN_REMOTE: self._block_run_remote,
            ShutdownSendLeaf.LAUNCH_CONDUCTOR: self._block_launch_conductor,
            ShutdownSendLeaf.RUN_NOTIFIER: self._block_run_notifier,
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

    def _block_launch_conductor(self, login: str, label: str, here: bool = True) -> Never:
        del label, here
        self._reached_send_leaf(
            ShutdownSendLeaf.LAUNCH_CONDUCTOR, f"conductor for {login}"
        )

    def _block_run_notifier(self, verb: str, instance: str) -> Never:
        del verb
        self._reached_send_leaf(ShutdownSendLeaf.RUN_NOTIFIER, f"notifier {instance}")
