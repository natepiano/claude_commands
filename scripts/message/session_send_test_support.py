"""Prevent tests from starting the real session sender."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import shlex
import sys
from typing import cast


REAL_SEND_SCRIPT = Path(__file__).resolve().with_name("send.py")
_guard_installed = False


class RealSenderForbidden(AssertionError):
    """A test requested the real session sender."""


@dataclass(frozen=True)
class NoSenderViolation:
    """No test requested the real session sender."""


@dataclass(frozen=True)
class RealSenderRefused:
    test_id: str


sender_violation: NoSenderViolation | RealSenderRefused = NoSenderViolation()


def requesting_test() -> str:
    """Find the test that requested the sender."""
    import unittest

    frame = inspect.currentframe()
    try:
        while frame is not None:
            candidate: object = frame.f_locals.get("self")
            if isinstance(candidate, unittest.TestCase):
                return candidate.id()
            if frame.f_code.co_name in ("setUpModule", "tearDownModule", "setUpClass", "tearDownClass"):
                module_name = cast(object, frame.f_globals["__name__"])
                return f"{module_name}.{frame.f_code.co_name}"
            frame = frame.f_back
    finally:
        del frame
    return "unittest fixture"


def _forbid_sender(event: str, arguments: tuple[object, ...]) -> None:
    global sender_violation
    directory: object = None
    if event == "subprocess.Popen":
        executable, command, directory = arguments[:3]
    elif event in ("os.posix_spawn", "os.exec"):
        executable, command = arguments[:2]
    elif event == "os.spawn":
        executable, command = arguments[1:3]
    elif event == "os.system":
        executable, command = "", arguments[0]
    else:
        return
    values = [executable]
    if isinstance(command, (list, tuple)):
        values.extend(cast(Sequence[object], command))
    else:
        values.append(command)
    base = Path(os.fsdecode(directory)) if isinstance(directory, (str, bytes, os.PathLike)) else Path.cwd()
    for value in values:
        if isinstance(value, (str, bytes, os.PathLike)):
            text = os.fsdecode(value)
            try:
                parts = shlex.split(text)
            except ValueError:
                continue
            for part in (text, *parts):
                path = Path(part)
                if not path.is_absolute():
                    path = base / path
                try:
                    resolved = path.resolve()
                except (OSError, ValueError):
                    continue
                if resolved == REAL_SEND_SCRIPT:
                    if isinstance(sender_violation, NoSenderViolation):
                        sender_violation = RealSenderRefused(requesting_test())
                    raise RealSenderForbidden(f"{sender_violation.test_id}: real session sender forbidden in tests: {path}")


def forbid_real_sender() -> None:
    """Install one process audit hook before any test can start the sender."""
    global _guard_installed
    if not _guard_installed:
        sys.addaudithook(_forbid_sender)
        _guard_installed = True
