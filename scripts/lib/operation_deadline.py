"""Share one monotonic deadline through nested lifecycle operations."""

import math
import time
from collections.abc import Generator
from contextlib import contextmanager
from scripts.agents import worker_lifecycle


def remaining(cap: float = math.inf) -> float:
    """Return the current cap, limited by the operation deadline."""
    deadline = worker_lifecycle.effective_deadline()
    if deadline == math.inf:
        return cap
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("deadline passed")
    return min(cap, left)


@contextmanager
def within(deadline: float) -> Generator[None]:
    """Retain the earlier deadline until the nested operation returns."""
    with worker_lifecycle.operation_deadline(deadline=deadline):
        yield


def check() -> None:
    """Refuse a signal or process start after the operation deadline."""
    if time.monotonic() >= worker_lifecycle.effective_deadline():
        raise TimeoutError("deadline passed")
