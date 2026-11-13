"""Keep repeat decisions independent of interpreter startup under test load."""

from __future__ import annotations

import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from types import SimpleNamespace
from threading import Thread
from unittest.mock import patch

from scripts.message import repeat_guard
from scripts.message.process_test_support import OwnedTestProcesses
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS


CHILD_TEST_SETUP = '''import runpy, sys
startup = runpy.run_path({startup!r}, run_name='_repeat_python_startup')
startup['select_checkout'](sys.argv)
startup['next_sitecustomize'](__file__)
from scripts.message import repeat_guard
repeat_guard.GUARD_IO_CEILING_SECONDS = {budget}
startup['finish_startup'](__file__, sys.argv)
'''


def install_guard_test_budget() -> None:
    """Give this module and its child interpreters the shared test hang limit."""
    root = Path(__file__).resolve().parents[2]
    _ = unittest.enterModuleContext(cast(AbstractContextManager[object], patch.object(
        repeat_guard, "GUARD_IO_CEILING_SECONDS", TEST_HANG_GUARD_SECS)))
    folder = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = (folder / "sitecustomize.py").write_text(CHILD_TEST_SETUP.format(
        budget=TEST_HANG_GUARD_SECS, startup=str(root / "scripts/lib/python_startup/sitecustomize.py")))
    from scripts.lib.python_startup.sitecustomize import inherited_paths

    paths = [str(folder), *inherited_paths(os.environ.get("PYTHONPATH", ""))]
    _ = unittest.enterModuleContext(cast(AbstractContextManager[object], patch.dict(
        os.environ, {"PYTHONPATH": os.pathsep.join(paths)})))


def install_notice_test_config(home: Path) -> Path:
    """Give notice fixtures real settings without repository recovery."""
    config = home / ".claude/config"
    config.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[2] / "config/send.conf"
    _ = shutil.copyfile(source, config / "send.conf")
    return config


def wait_for_notice_readiness(path: Path, runner: subprocess.Popen[bytes]) -> None:
    """Fail when the runner exits before its descendants report readiness."""
    exited, notification = socket.socketpair()

    def report_exit() -> None:
        with notification:
            _ = runner.wait()
            try:
                _ = notification.send(b"exit")
            except BrokenPipeError:
                pass  # The readiness wait already ended.

    watcher = Thread(target=report_exit, daemon=True)
    with exited, ExitStack() as contexts:
        ready = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        _ = contexts.callback(os.close, ready)
        watcher.start()
        received = b""
        with LauncherHangGuard("delivery descendants").protect():
            while b"\n" not in received:
                readable, _, _ = select.select([ready, exited], [], [])
                if ready in readable:
                    received += os.read(ready, len(b"ready\n"))
                if b"\n" not in received and exited in readable:
                    watcher.join()
                    raise AssertionError(f"delivery runner exited {runner.returncode} before readiness")
        if received != b"ready\n":
            raise AssertionError(f"delivery descendants reported invalid readiness: {received!r}")


def wait_for_notice_exit(pid: int) -> None:
    """Wait for death without sending a signal or changing the process state."""
    try:
        with LauncherHangGuard("delivery descendant exit").protect():
            if sys.platform == "linux":
                descriptor = os.pidfd_open(pid)
                try:
                    _ = select.select([descriptor], [], [])
                finally:
                    os.close(descriptor)
            else:
                queue = select.kqueue()
                try:
                    event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                          flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)
                    _ = queue.control([event], 1)
                finally:
                    queue.close()
    except ProcessLookupError:
        pass  # The process exited before the event subscription.


@dataclass
class NoticeDrainClock:
    """Advance admission time only after a notice attempt completes."""

    elapsed: float = 0

    def monotonic(self) -> float:
        return self.elapsed


@dataclass
class NoticeAttemptTrace:
    sender: str
    notice_id: str
    marker: str
    outcome: str = "not completed"


def run_ready_notice_attempt(
    run: Callable[[list[str], str, float], tuple[int | None, str, str]],
    command: list[str], request: str, timeout: float, marker: str,
) -> tuple[int | None, str, str]:
    """Time out a real blocked child only after its readiness signal arrives."""
    launch = subprocess.Popen
    owned = OwnedTestProcesses()
    attempt_timeout = timeout
    read_fd, write_fd = os.pipe()
    writer_open = True
    with os.fdopen(read_fd) as ready, ExitStack() as contexts:
        def start(arguments: list[str], *, stdin: int, stdout: int, stderr: int,
                  text: Literal[True], env: dict[str, str] | None, cwd: Path | None,
                  start_new_session: bool) -> subprocess.Popen[str]:
            nonlocal writer_open
            child = launch(arguments, stdin=stdin, stdout=stdout, stderr=stderr, text=text,
                           env=owned.environment({**(os.environ if env is None else env), "NOTICE_READY_FD": str(write_fd)}),
                           cwd=cwd, start_new_session=start_new_session, pass_fds=(write_fd,))
            owned.track(child, group=start_new_session)
            os.close(write_fd)
            writer_open = False
            communicate = child.communicate
            first = True

            def exchange(input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
                nonlocal first
                if first and marker:
                    first = False
                    try:
                        if child.stdin is None:
                            raise AssertionError("notice attempt has no request pipe")
                        _ = child.stdin.write(input or "")
                        child.stdin.close()
                        child.stdin = None
                        with LauncherHangGuard("notice blocking marker").protect():
                            actual = ready.readline().strip()
                        if actual != marker:
                            raise AssertionError(f"expected {marker}, received {actual!r}")
                    except BaseException:
                        owned.finish()
                        raise
                    raise subprocess.TimeoutExpired(arguments, attempt_timeout if timeout is None else timeout)
                return communicate(input, timeout=TEST_HANG_GUARD_SECS)

            _ = contexts.enter_context(patch.object(child, "communicate", exchange))
            return child

        try:
            processes = SimpleNamespace(Popen=start, PIPE=subprocess.PIPE,
                                        TimeoutExpired=subprocess.TimeoutExpired)
            _ = contexts.enter_context(patch.object(sys.modules[run.__module__], "subprocess", processes))
            return run(command, request, timeout)
        finally:
            if writer_open:
                os.close(write_fd)
            owned.finish()
