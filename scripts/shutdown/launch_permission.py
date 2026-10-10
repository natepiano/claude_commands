#!/usr/bin/env python3
"""Refuse new Claude work while an account is held by shutdown state."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class NewWorkLaunchPurpose:
    """A launch unrelated to restoring a recorded shutdown session."""


@dataclass(frozen=True)
class ShutdownUnitRestoreLaunchPurpose:
    """A launch that restores one unit recorded by shutdown."""

    session_id: str


LaunchPurpose = NewWorkLaunchPurpose | ShutdownUnitRestoreLaunchPurpose


@dataclass(frozen=True)
class AllowedByShutdownState:
    """The current shutdown state permits this launch."""


@dataclass(frozen=True)
class BlockedByShutdown:
    """A readable shutdown record holds this launch."""

    reason: str


@dataclass(frozen=True)
class ShutdownStateUnreadable:
    """Shutdown state could not safely decide whether to permit a launch."""

    detail: str


LaunchPermission = (
    AllowedByShutdownState | BlockedByShutdown | ShutdownStateUnreadable
)

_TIMEOUT_SECONDS = 30
_UNREADABLE_PREFIX = "shutdown state unreadable: "


def shutdown_state_root() -> Path:
    """The machine-local root shared by shutdown records and launch locking."""
    return Path(
        os.environ.get(
            "SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")
        )
    )


@contextmanager
def _held_launch_barrier() -> Generator[None, None, None]:
    root = shutdown_state_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "launch.lock").open("a+", encoding="utf-8") as lock_file:
        os.set_inheritable(lock_file.fileno(), False)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def launch_barrier() -> AbstractContextManager[None]:
    """Hold the barrier that serializes launch decisions with shutdown creation."""
    return _held_launch_barrier()


def _first_line(text: str) -> str:
    lines = text.splitlines()
    return lines[0] if lines else ""


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _unreadable_detail(status: int, stderr: str) -> str:
    detail = _first_line(stderr)
    if detail.startswith(_UNREADABLE_PREFIX):
        detail = detail.removeprefix(_UNREADABLE_PREFIX)
    return detail or f"launch-blocked exited {status}"


def launch_permission(
    environment: Mapping[str, str], purpose: LaunchPurpose
) -> LaunchPermission:
    """Ask shutdown.py whether `purpose` may launch in `environment`."""
    command = [
        sys.executable,
        str(Path(__file__).with_name("shutdown.py")),
        "launch-blocked",
    ]
    if isinstance(purpose, ShutdownUnitRestoreLaunchPurpose):
        command.extend(("--restart-of", purpose.session_id))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
            env=environment,
        )
    except OSError as error:
        return ShutdownStateUnreadable(str(error))
    except subprocess.TimeoutExpired:
        return ShutdownStateUnreadable(
            f"launch-blocked timed out after {_TIMEOUT_SECONDS} s"
        )
    if completed.returncode == 1:
        if not completed.stdout and not completed.stderr:
            return AllowedByShutdownState()
        detail = _last_nonempty_line(completed.stderr) or _first_line(
            completed.stdout
        )
        return ShutdownStateUnreadable(f"launch-blocked exited 1: {detail}")
    if completed.returncode == 0:
        return BlockedByShutdown(_first_line(completed.stdout))
    return ShutdownStateUnreadable(
        _unreadable_detail(completed.returncode, completed.stderr)
    )


class CommandLine(argparse.Namespace):
    command_name: str = ""
    name: str = ""
    launched_command: list[str] = []


def _exec_new_work(name: str, command: list[str]) -> int:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("exec-new-work requires a command after --")
    try:
        barrier = launch_barrier()
        _ = barrier.__enter__()
    except OSError as error:
        print(
            f"{name}: skipped: shutdown state unreadable: launch barrier: {error}"
        )
        return 0
    try:
        permission = launch_permission(os.environ, NewWorkLaunchPurpose())
        if isinstance(permission, BlockedByShutdown):
            print(f"{name}: skipped: {permission.reason}")
            return 0
        if isinstance(permission, ShutdownStateUnreadable):
            print(
                f"{name}: skipped: shutdown state unreadable: {permission.detail}"
            )
            return 0
        os.execv(command[0], command)
    finally:
        _ = barrier.__exit__(None, None, None)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command_name", required=True)
    execute = commands.add_parser("exec-new-work")
    _ = execute.add_argument("--name", required=True)
    _ = execute.add_argument("launched_command", nargs=argparse.REMAINDER)
    options = cast(CommandLine, parser.parse_args(arguments))
    try:
        return _exec_new_work(options.name, options.launched_command)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
