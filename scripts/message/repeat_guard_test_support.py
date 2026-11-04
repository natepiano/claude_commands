"""Keep repeat decisions independent of interpreter startup under test load."""

from __future__ import annotations

import os
import importlib
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

from scripts.message.process_test_support import OwnedTestProcesses
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS


CHILD_TEST_SETUP = '''import importlib, os, runpy, sys
from importlib.machinery import PathFinder

# Preserve the system setup before applying the test budget.
folder = os.path.dirname(os.path.abspath(__file__))
paths = [entry for entry in sys.path if os.path.abspath(entry) != folder]
original = PathFinder.find_spec('sitecustomize', paths)
if original is not None and original.origin is not None:
    runpy.run_path(original.origin, run_name='_system_sitecustomize')
for name in ('repeat_guard', 'scripts.message.repeat_guard'):
    importlib.import_module(name).GUARD_IO_CEILING_SECONDS = {budget}
'''


def install_guard_test_budget() -> None:
    """Give this module and its child interpreters the shared test hang limit."""
    root = Path(__file__).resolve().parents[2]
    _ = unittest.enterModuleContext(cast(AbstractContextManager[object], patch.object(
        sys, "path", [str(root / "scripts/message"), str(root), *sys.path])))
    for name in ("repeat_guard", "scripts.message.repeat_guard"):
        module = importlib.import_module(name)
        _ = unittest.enterModuleContext(cast(AbstractContextManager[object], patch.object(
            module, "GUARD_IO_CEILING_SECONDS", TEST_HANG_GUARD_SECS)))
    folder = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = (folder / "sitecustomize.py").write_text(CHILD_TEST_SETUP.format(budget=TEST_HANG_GUARD_SECS))
    paths = [str(folder), str(root), str(root / "scripts/message")]
    if existing := os.environ.get("PYTHONPATH"):
        paths.append(existing)
    _ = unittest.enterModuleContext(cast(AbstractContextManager[object], patch.dict(
        os.environ, {"PYTHONPATH": os.pathsep.join(paths)})))


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
            _ = contexts.enter_context(patch.object(subprocess, "Popen", start))
            return run(command, request, timeout)
        finally:
            if writer_open:
                os.close(write_fd)
            owned.finish()
