"""Stop one operation at a selected durable boundary."""
from __future__ import annotations

import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Literal, cast
from unittest.mock import patch


class InjectedStop(BaseException):
    """The test interrupts work without invoking ordinary error recovery."""


@contextmanager
def stop_once(target: object, attribute: str, *, when: Literal["before", "after"] = "after",
              argument: int = 0, matches: Callable[[object], bool] | None = None,
              terminate: Callable[[], None] | None = None) -> Generator[None]:
    operation = cast(Callable[..., object], getattr(target, attribute))
    stopped = False

    def interrupt() -> None:
        if terminate is not None:
            terminate()
        raise InjectedStop(attribute)

    def wrapped(*args: object, **kwargs: object) -> object:
        nonlocal stopped
        selected = not stopped and (matches is None or (len(args) > argument and matches(args[argument])))
        if selected:
            stopped = True
            if when == "before":
                interrupt()
        result = operation(*args, **kwargs)
        if selected:
            interrupt()
        return result

    with patch.object(target, attribute, wrapped):
        yield


def stop_in_child(action: Callable[[], object], target: object, attribute: str, *,
                  when: Literal["before", "after"] = "after", argument: int = 0,
                  matches: Callable[[object], bool] | None = None) -> None:
    """Terminate a child at the boundary; the parent can retry persisted work."""
    stopped_status = 86
    child = os.fork()
    if child == 0:
        try:
            with stop_once(target, attribute, when=when, argument=argument, matches=matches,
                           terminate=lambda: os._exit(stopped_status)):
                _ = action()
        finally:
            os._exit(87)
    observed, status = os.waitpid(child, 0)
    if observed != child or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != stopped_status:
        raise AssertionError(f"child missed the stop at {attribute}: status {status}")
