"""Reject unsafe process IDs before a test can reach an operating-system signal."""
from __future__ import annotations

import os
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import patch

from scripts.lib import process_group


@dataclass(frozen=True)
class SignalPidGuard:
    send: Callable[[int, int], None]

    def __call__(self, pid: object, signum: int) -> None:
        if type(pid) is not int or pid <= 1:
            raise AssertionError(f"refusing unsafe signal target: {pid!r}")
        self.send(pid, signum)


def install_signal_pid_guard() -> None:
    """Guard both signal boundaries for a unittest module, including its cleanup."""
    _ = unittest.enterModuleContext(patch.object(os, "kill", new=SignalPidGuard(os.kill)))
    _ = unittest.enterModuleContext(patch.object(
        process_group, "send_process_group", new=SignalPidGuard(process_group.send_process_group)))
