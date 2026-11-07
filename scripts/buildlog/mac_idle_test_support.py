"""Private idle fixtures and notification transport guards for integration tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections.abc import Callable, Generator, Sequence
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast, override
from unittest import mock
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

if TYPE_CHECKING:
    from ..lint.memory_test_support import write_config_fixture
else:
    from scripts.lint.memory_test_support import write_config_fixture


def write_idle_config(directory: Path, overrides: str = "") -> Path:
    """Write a complete private configuration with the shared value limits."""
    path = directory / "mac_test.conf"
    _ = write_config_fixture(path, overrides)
    return path


def write_idle_alert(directory: Path, now: float, *, written_age: float = 0) -> Path:
    """One saved blocked-Mac fixture for both queue and target view tests."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "alert.json"
    _ = path.write_text(json.dumps({
        "version": 1, "written": now - written_age, "state": "idle", "since": now - 600,
        "waiting": 7, "cause": "blocked", "holder": "research", "detail": "P15 F11 L0 run",
        "until": 0, "messaged": True, "pushed": False,
    }))
    return path



class NotificationTransportBlocked(AssertionError):
    """A test attempted to reach a real notification or address resolver."""


P = ParamSpec("P")
R = TypeVar("R")


def reject_notification_command(command: object) -> None:
    words = ([command] if isinstance(command, str) else
             cast(Sequence[object], command) if isinstance(command, (tuple, list)) else ())
    for word in words:
        text = str(word)
        if "message/send.py" in text or "production/showrunners.py" in text:
            raise NotificationTransportBlocked(f"Real test notification transport forbidden: {text}")


_guarded_tests: set[unittest.TestCase] = set()


def notification_spawn_guard(event: str, args: tuple[object, ...]) -> None:
    """The spawn boundary remains protected when tests restore subprocess functions."""
    if _guarded_tests and event == "subprocess.Popen":
        reject_notification_command(args[1])


sys.addaudithook(notification_spawn_guard)


def guarded_transport(transport: Callable[P, R]) -> Callable[P, R]:
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        command = args[0] if args else kwargs.get("args")
        reject_notification_command(command)
        return transport(*args, **kwargs)
    return guarded


class NotificationGuardedTestCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _guarded_tests.add(self)
        self.addCleanup(_guarded_tests.remove, self)
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=guarded_transport(subprocess.run)))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=guarded_transport(subprocess.Popen)))


if TYPE_CHECKING:
    from ..message import send, sessions
    from ..lib import process_start
    from ..production import staff
else:
    from scripts.message import send, sessions
    from scripts.lib import process_start
    from scripts.production import staff


def no_checkpoint() -> None:
    pass


@dataclass
class PrivateSenderAcceptance:
    """Real sender state with only discovery and transport replaced by private fixtures."""
    state: Path
    accepted: Path
    owner_start: str = "fixture-owner"

    def end_owner(self) -> None:
        """Replace the sender lifetime after a simulated process stop."""
        self.owner_start += "-replacement"

    def count(self) -> int:
        return len(self.accepted.read_text().splitlines()) if self.accepted.exists() else 0

    def options(self, channel: str, *, address: str = "uds:/fixture/builds.sock") -> send.Options:
        arguments = ["--to", "user" if channel == "push" else address, "--from", "fixture",
                     "--summary", "Waiting work", "--text", "Work waits for the Mac.",
                     "--key", "outage", "--repeat-minutes", "1440",
                     "--recipient-key", "user" if channel == "push" else address]
        if channel == "push":
            arguments += ["--text-id", "nate.adhoc", "--value", "sender=fixture",
                          "--value", "title=Waiting work", "--value", "description=Work waits for the Mac.", "--no-action"]
        return send.parse(arguments)._replace(shown_sender=send.ShownSender("fixture"))


@contextmanager
def private_sender_acceptance(state: Path, *, before_accept: Callable[[], None] = no_checkpoint,
                              after_accept: Callable[[], None] = no_checkpoint) -> Generator[PrivateSenderAcceptance]:
    """No real phone, socket, subprocess or recipient registry is reachable through these fakes."""
    state.mkdir(parents=True, exist_ok=True)
    fixture = PrivateSenderAcceptance(state, state / "acceptances.jsonl")
    owner_pid = os.getpid()
    real_process_start = process_start.process_start

    def sender_process_start(pid: int) -> process_start.ProcessStart:
        if pid == owner_pid:
            return process_start.Started(fixture.owner_start)
        return real_process_start(pid)

    def transport(command: list[str], _stdin: str, _timeout: float) -> tuple[int, str, str]:
        before_accept()
        with fixture.accepted.open("a") as accepted:
            _ = accepted.write(json.dumps(command) + "\n")
        after_accept()
        return 0, "accepted by private transport", ""

    def discover(address: str, _scope: send.RecipientResolutionScope,
                 _observed: object, **_kwargs: object) -> send.DeliveryRoute:
        return send.CodexWorkerRecipient(address, "fixture", str(state / "seat"))

    with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())), \
         mock.patch.object(process_start, "process_start", side_effect=sender_process_start), \
         mock.patch.object(sessions, "running_sessions", return_value=[]), \
         mock.patch.object(send, "STATE", state), mock.patch.object(send, "wrong_template", return_value=None), mock.patch.object(send, "run", side_effect=transport), \
         mock.patch.object(send, "resolve_recipient", side_effect=discover):
        yield fixture
