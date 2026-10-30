"""Interrupt a real operation once at a chosen durable-state boundary."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
import os
import traceback
from typing import Literal, Never, cast
from unittest.mock import patch


class InjectedStop(BaseException):
    """A stopped command bypasses ordinary error recovery, like process death."""


def _raise_stop() -> Never:
    raise InjectedStop()


@contextmanager
def stop_once(target: object, attribute: str, *, when: Literal["before", "after"] = "after",
              matches: Callable[[object], bool] = lambda _value: True, argument: int = 0,
              interrupt: Callable[[], Never] = _raise_stop) -> Generator[None]:
    """Keep real side effects; stop only the first matching call before or after it."""
    operation = cast(Callable[..., object], getattr(target, attribute))
    fired = False

    def wrapped(*args: object, **kwargs: object) -> object:
        nonlocal fired
        selected = not fired and matches(args[argument])
        if selected:
            fired = True
            if when == "before":
                interrupt()
        result = operation(*args, **kwargs)
        if selected:
            interrupt()
        return result

    with patch.object(target, attribute, new=wrapped):
        yield


def stop_in_child(operation: Callable[[], object], target: object, attribute: str, *,
                  when: Literal["before", "after"] = "after",
                  matches: Callable[[object], bool] = lambda _value: True, argument: int = 0) -> None:
    """Leave real crash residue with a dead owner, rather than a live test-process PID."""
    pid = os.fork()
    if pid == 0:
        try:
            with stop_once(target, attribute, when=when, matches=matches, argument=argument,
                           interrupt=lambda: os._exit(83)):
                _ = operation()
        except BaseException:
            traceback.print_exc()
        os._exit(84)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) != 83:
        raise AssertionError(f"child did not reach the stop at {attribute}: status {status}")
