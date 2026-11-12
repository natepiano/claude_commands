"""Own forked test children, their process groups, and their output files."""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import traceback
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from multiprocessing.context import ForkContext
from multiprocessing.process import BaseProcess
from tempfile import TemporaryFile
from types import TracebackType
from typing import TextIO, override

from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def _run(action: Callable[[], int], stdout: TextIO, stderr: TextIO, close_fds: tuple[int, ...]) -> None:
    status = 1
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                os.setpgid(0, 0)
                for descriptor in close_fds:
                    os.close(descriptor)
                _ = os.dup2(stdout.fileno(), 1)
                _ = os.dup2(stderr.fileno(), 2)
                status = action()
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
                if isinstance(error.code, str):
                    print(error.code, file=stderr)
            except BaseException:
                traceback.print_exc(file=stderr)
            finally:
                stdout.flush()
                stderr.flush()
    finally:
        os._exit(status)


@dataclass(frozen=True)
class ForkChild(AbstractContextManager["ForkChild"]):
    """One child whose lifetime and output files belong to a ForkChildren scope."""

    _process: BaseProcess
    _stdout: TextIO
    _stderr: TextIO

    @property
    def pid(self) -> int:
        pid = self._process.pid
        if pid is None:
            raise RuntimeError("The child has not started.")
        return pid

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def result(self, timeout: float = TEST_HANG_GUARD_SECS) -> subprocess.CompletedProcess[str]:
        self._process.join(timeout=timeout)
        status = self._process.exitcode
        if status is None:
            raise TimeoutError("The forked test child did not finish.")
        _ = self._stdout.seek(0)
        _ = self._stderr.seek(0)
        return subprocess.CompletedProcess([], status, self._stdout.read(), self._stderr.read())

    @override
    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> None:
        del kind, error, trace
        pid = self._process.pid
        if pid is None:
            self._process.close()
            return
        try:
            try:
                send_process_group(pid, signal.SIGKILL)
            except ProcessLookupError:
                if self._process.is_alive():
                    self._process.kill()
        finally:
            self._process.join(timeout=TEST_HANG_GUARD_SECS)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=TEST_HANG_GUARD_SECS)
            if self._process.is_alive():
                raise TimeoutError("The forked test child did not stop.")
            self._process.close()


class ForkChildren(AbstractContextManager["ForkChildren"]):
    """Release all children and descriptors when the test leaves this scope."""

    def __init__(self) -> None:
        self._scope: ExitStack = ExitStack()
        self._context: ForkContext = multiprocessing.get_context("fork")
        self._descriptors: set[int] = set()
        self._streams: list[TextIO] = []

    @override
    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> bool | None:
        return self._scope.__exit__(kind, error, trace)

    def _close_descriptor(self, descriptor: int) -> None:
        if descriptor in self._descriptors:
            self._descriptors.remove(descriptor)
            os.close(descriptor)

    def pipe(self) -> tuple[int, int]:
        """Own both pipe descriptors before a child can start."""
        reader, writer = os.pipe()
        self._descriptors.update((reader, writer))
        _ = self._scope.callback(self._close_descriptor, reader)
        _ = self._scope.callback(self._close_descriptor, writer)
        return reader, writer

    def start(self, action: Callable[[], int], *, close_fds: Iterable[int] = (),
              pass_fds: Iterable[int] = ()) -> ForkChild:
        """Keep passed descriptors in the child and close them in the parent."""
        stdout = self._scope.enter_context(TemporaryFile(mode="w+"))
        stderr = self._scope.enter_context(TemporaryFile(mode="w+"))
        passed = tuple(pass_fds)
        inherited = self._descriptors | {stream.fileno() for stream in self._streams if not stream.closed}
        inherited.difference_update(passed)
        inherited.update(close_fds)
        self._streams.extend((stdout, stderr))
        process = self._context.Process(target=_run, args=(action, stdout, stderr, tuple(sorted(inherited))))
        child = ForkChild(process, stdout, stderr)
        _ = self._scope.enter_context(child)
        try:
            process.start()
            try:
                os.setpgid(child.pid, child.pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                try:
                    group = os.getpgid(child.pid)
                except ProcessLookupError:
                    pass
                else:
                    if group != child.pid:
                        raise
        finally:
            for descriptor in passed:
                self._close_descriptor(descriptor)
        return child
