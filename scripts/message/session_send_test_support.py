"""Prevent tests from starting the real session sender."""
from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import sys
from typing import cast

from scripts.message import session_send


REAL_SEND_SCRIPT = Path(session_send.__file__).resolve().with_name("send.py")
_guard_installed = False


def _forbid_sender(event: str, arguments: tuple[object, ...]) -> None:
    if event != "subprocess.Popen":
        return
    executable, command, directory = arguments[:3]
    values = [executable]
    if isinstance(command, (list, tuple)):
        values.extend(cast(Sequence[object], command))
    else:
        values.append(command)
    base = Path(os.fsdecode(directory)) if isinstance(directory, (str, bytes, os.PathLike)) else Path.cwd()
    for value in values:
        if isinstance(value, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(value))
            if not path.is_absolute():
                path = base / path
            try:
                resolved = path.resolve()
            except (OSError, ValueError):
                continue
            if resolved == REAL_SEND_SCRIPT:
                raise AssertionError(f"real session sender forbidden in tests: {path}")


def forbid_real_sender() -> None:
    """Install one process audit hook before any test can start the sender."""
    global _guard_installed
    if not _guard_installed:
        sys.addaudithook(_forbid_sender)
        _guard_installed = True
