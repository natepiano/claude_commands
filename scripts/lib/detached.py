"""Start a detached child and confirm its first sign of life."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


STARTUP_SIGNAL_ENV = "DETACHED_STARTUP_SIGNAL"


@dataclass(frozen=True)
class DiscardOutput:
    """The child needs no retained output."""


@dataclass(frozen=True)
class AppendLog:
    path: Path


@dataclass(frozen=True)
class InheritEnvironment:
    """The child uses its parent's environment."""


@dataclass(frozen=True)
class ExplicitEnvironment:
    mapping: Mapping[str, str]


_DISCARD_OUTPUT = DiscardOutput()
_INHERIT_ENVIRONMENT = InheritEnvironment()


class DetachedStartFailure(Exception):
    """A detached program could not start, exited, or never became ready."""


class DetachedChildExited(DetachedStartFailure):
    """The child exited before readiness; its pid can distinguish a competing owner."""

    def __init__(self, program: str, pid: int, status: int) -> None:
        super().__init__(f"{program} did not start: exited with status {status}")
        self.pid: int = pid


def _terminate_group(child: subprocess.Popen[bytes]) -> None:
    """Reap our child and stop descendants that share its private group."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        _ = child.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _ = child.wait(timeout=0.5)


def start_detached(argv: Sequence[str], *, ready: Callable[[], bool], within: float,
                   output: DiscardOutput | AppendLog = _DISCARD_OUTPUT,
                   environment: InheritEnvironment | ExplicitEnvironment = _INHERIT_ENVIRONMENT) -> int:
    """Return the private-session child's pid after readiness while it is alive.

    Readiness probes must bound their own blocking work to the startup deadline.
    Output is either appended to the named log or discarded.
    A failed or interrupted readiness probe stops this attempt's process group.
    """
    program = argv[0] if argv else "program"
    if not argv or not math.isfinite(within) or within <= 0:
        raise DetachedStartFailure(f"{program} did not start: startup time must be positive and finite")
    deadline = time.monotonic() + within
    try:
        with output.path.open("ab") if isinstance(output, AppendLog) else nullcontext(subprocess.DEVNULL) as stream:
            child = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=stream, close_fds=True,
                                     env=environment.mapping if isinstance(environment, ExplicitEnvironment) else None)
    except OSError as error:
        raise DetachedStartFailure(f"{program} did not start: {error}") from error
    try:
        while True:
            status = child.poll()
            if status is not None:
                raise DetachedChildExited(program, child.pid, status)
            if time.monotonic() >= deadline:
                raise DetachedStartFailure(f"{program} did not start: no readiness within {within:g} seconds")
            if ready():
                status = child.poll()
                if status is not None:
                    raise DetachedChildExited(program, child.pid, status)
                if time.monotonic() < deadline:
                    # Keep ownership of the Popen object until its eventual exit,
                    # so successful launches are reaped without leaving zombies.
                    threading.Thread(target=child.wait, daemon=True).start()
                    return child.pid
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
    except BaseException:
        _terminate_group(child)
        raise
