"""Verify the Mac copy with private state and fake process runners."""
from __future__ import annotations

import fcntl
import io
import os
import signal
import subprocess
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.agents import remote

copy_to_mac = cast(Callable[..., None], getattr(staff, "_copy_to_mac"))


@dataclass
class CopyRunner:
    """Record process calls without starting any process."""

    launches: list[dict[str, object]] = field(default_factory=list)
    sends: list[tuple[list[str], dict[str, object]]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    on_launch: Callable[[], None] = lambda: None
    on_send: Callable[[], None] = lambda: None
    on_stop: Callable[[], None] = lambda: None
    handler: Callable[[int, object], None] = lambda number, frame: None
    blocked: set[signal.Signals] = field(default_factory=set)
    status: int = 0
    reason: str = ""

    def launch(self, arguments: list[str], **kwargs: object) -> "CopyProcess":
        if arguments[0] != "ssh":
            self.launches.append(kwargs)
            self.on_launch()
        return CopyProcess(self, arguments)

    def send(self, arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.sends.append((arguments, kwargs))
        self.on_send()
        return subprocess.CompletedProcess(arguments, self.status, "", self.reason)

    def set_handler(self, number: int, handler: Callable[[int, object], None]) -> Callable[[int, object], None]:
        del number
        previous = self.handler
        self.handler = handler
        return previous

    def mask_signals(self, how: int, blocked: set[signal.Signals]) -> set[signal.Signals]:
        previous = self.blocked.copy()
        if how == signal.SIG_BLOCK:
            self.blocked.update(blocked)
        else:
            self.blocked = blocked.copy()
        return previous


@dataclass
class CopyProcess:
    """Model an SSH child and its completion under the copy lock."""

    runner: CopyRunner
    arguments: list[str]
    returncode: int = 0

    def communicate(self, input: str, timeout: float) -> tuple[str, str]:
        result = self.runner.send(self.arguments, input=input, timeout=timeout)
        self.returncode = result.returncode
        return result.stdout, result.stderr

    def terminate(self) -> None:
        self.runner.events.append("terminate")
        self.runner.on_stop()

    def wait(self) -> int:
        self.runner.events.append("wait")
        self.runner.on_stop()
        return self.returncode

    def __enter__(self) -> "CopyProcess":
        return self

    def __exit__(self, kind: object, error: object, traceback: object) -> None:
        del kind, error, traceback
        _ = self.wait()


class MacCopyTests(StaffTestCase):
    runner: CopyRunner = CopyRunner()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.runner = CopyRunner()
        _ = self.enterContext(cast(AbstractContextManager[object],
            mock.patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"})))
        # Every process boundary is fake, even when a test removes STAFF_FILE.
        _ = self.enterContext(cast(AbstractContextManager[object],
            mock.patch.object(subprocess, "Popen", self.runner.launch)))
        _ = self.enterContext(cast(AbstractContextManager[object],
            mock.patch.object(subprocess, "run", self.runner.send)))
        _ = self.enterContext(cast(AbstractContextManager[object],
            mock.patch.object(signal, "signal", self.runner.set_handler)))
        _ = self.enterContext(cast(AbstractContextManager[object],
            mock.patch.object(signal, "pthread_sigmask", self.runner.mask_signals)))

    def use_default(self) -> Path:
        _ = os.environ.pop("STAFF_FILE")
        return Path.home() / ".local/state/staff/staff.json"

    def test_default_write_launches_after_the_record_exists(self) -> None:
        path = self.use_default()
        written: list[str] = []

        def launch() -> None:
            written.append(path.read_text())
            errors = cast(io.TextIOWrapper, self.runner.launches[-1]["stderr"])
            self.assertEqual(errors.name, str(path.with_name("mac-copy.log")))

        self.runner.on_launch = launch
        row = staff.ensure("showrunner", name="copy", runtime_id="019b3a40-4250-7000-8000-000000000001")
        self.assertEqual(len(written), 1)
        self.assertIn(row.id, written[0])
        self.assertEqual(self.runner.sends, [])
        self.assertTrue(self.runner.launches[0]["start_new_session"])
        self.assertEqual(self.runner.launches[0]["stdin"], subprocess.DEVNULL)

    def test_private_record_never_launches_or_sends(self) -> None:
        _ = fixture_member("showrunner", name="private")
        copy_to_mac(detached=False)
        self.assertEqual(self.runner.launches, [])
        self.assertEqual(self.runner.sends, [])

    def test_mac_write_forwards_and_never_launches_a_copy(self) -> None:
        with self.machine("mac"):
            with mock.patch.object(staff, "_remote_row") as forward:
                _ = staff.ensure("showrunner", name="forward", runtime_id="unused")
                copy_to_mac(detached=False)
        forward.assert_called_once()
        self.assertEqual(self.runner.launches, [])
        self.assertEqual(self.runner.sends, [])

    def test_launch_failure_prints_one_line_and_preserves_the_write(self) -> None:
        path = self.use_default()

        def fail() -> None:
            raise OSError("cannot start\ncopy")

        self.runner.on_launch = fail
        errors = io.StringIO()
        with redirect_stderr(errors):
            row = staff.ensure("showrunner", name="survives", runtime_id="019b3a40-4250-7000-8000-000000000002")
        self.assertIn(row.id, path.read_text())
        self.assertEqual(errors.getvalue(), "staff: Mac copy failed: cannot start copy\n")

    def test_send_failure_prints_one_line_and_preserves_the_write(self) -> None:
        path = self.use_default()
        self.runner.status = 255
        self.runner.reason = "link failed\nmore detail\n"
        self.runner.on_launch = lambda: copy_to_mac(detached=False)
        errors = io.StringIO()
        with redirect_stderr(errors):
            row = staff.ensure("showrunner", name="survives", runtime_id="019b3a40-4250-7000-8000-000000000002")
        self.assertIn(row.id, path.read_text())
        self.assertEqual(errors.getvalue(), "staff: Mac copy failed: link failed more detail\n")

    def test_remote_command_uses_a_same_folder_temporary_and_atomic_rename(self) -> None:
        path = self.use_default()
        _ = staff.ensure("showrunner", name="atomic", runtime_id="019b3a40-4250-7000-8000-000000000003")
        copy_to_mac(detached=False)
        arguments, options = self.runner.sends[0]
        self.assertEqual(arguments[:-1], [
            "ssh", "-o", "BatchMode=yes", "mac",
        ])
        command = arguments[-1]
        self.assertIn('directory="$HOME/.local/state/staff"', command)
        self.assertIn('mktemp "$directory/staff.json.mac-copy.XXXXXXXX"', command)
        self.assertLess(command.index('cat > "$temporary"'), command.index('mv -f'))
        self.assertIn('mv -f "$temporary" "$directory/staff.json"', command)
        self.assertIn('trap \'rm -f "$temporary"\' EXIT', command)
        self.assertEqual(options["input"], path.read_text())

    def test_reversed_copy_launches_send_the_latest_record_under_one_lock(self) -> None:
        path = self.use_default()
        old = staff.ensure("showrunner", name="old", runtime_id="019b3a40-4250-7000-8000-000000000004")
        _ = staff.ensure("showrunner", name="new", runtime_id="019b3a40-4250-7000-8000-000000000005")
        self.assertEqual(len(self.runner.launches), 2)
        latest = path.read_text()

        def send() -> None:
            with path.with_name("mac-copy.lock").open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.runner.on_send = send
        # Start the newer copy first, then start the delayed older copy.
        copy_to_mac(detached=False)
        copy_to_mac(detached=False)
        received = [options["input"] for _, options in self.runner.sends]
        self.assertEqual(received, [latest, latest])
        self.assertIn(old.id, str(received[-1]))
        self.assertIn('"name": "new"', str(received[-1]))


    def test_termination_reaps_ssh_before_releasing_the_copy_lock(self) -> None:
        path = self.use_default()
        _ = staff.ensure("showrunner", name="terminated", runtime_id="019b3a40-4250-7000-8000-000000000006")
        previous_handler = self.runner.handler

        def stop() -> None:
            with path.with_name("mac-copy.lock").open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn(signal.SIGTERM, self.runner.blocked)

        self.runner.on_stop = stop
        self.runner.on_send = lambda: self.runner.handler(signal.SIGTERM, None)
        errors = io.StringIO()
        with redirect_stderr(errors):
            copy_to_mac(detached=False)
        self.assertEqual(self.runner.events[:2], ["terminate", "wait"])
        self.assertEqual(errors.getvalue(), "staff: Mac copy failed: copier terminated\n")
        self.assertIs(self.runner.handler, previous_handler)
        self.assertEqual(self.runner.blocked, set())
        with path.with_name("mac-copy.lock").open("a") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_timeout_reports_one_line_and_allows_the_next_copy(self) -> None:
        path = self.use_default()
        row = staff.ensure("showrunner", name="timeout", runtime_id="019b3a40-4250-7000-8000-000000000007")
        expired = subprocess.TimeoutExpired("ssh", remote.STANDARD_TIME_LIMIT["seconds"])

        def timeout() -> None:
            raise expired

        self.runner.on_send = timeout
        errors = io.StringIO()
        with redirect_stderr(errors):
            copy_to_mac(detached=False)
        self.assertEqual(self.runner.events[:2], ["terminate", "wait"])
        self.assertEqual(errors.getvalue(), f"staff: Mac copy failed: {expired}\n")
        self.assertIn(row.id, path.read_text())
        self.assertEqual(self.runner.sends[0][1]["timeout"], expired.timeout)
        with path.with_name("mac-copy.lock").open("a") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.runner.on_send = lambda: None
        copy_to_mac(detached=False)
        self.assertEqual(len(self.runner.sends), 2)


if __name__ == "__main__":
    _ = unittest.main()
