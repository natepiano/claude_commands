"""Pace crates.io requests to its published crawler limit of one request a second."""
from __future__ import annotations

import time
from collections.abc import Callable


CRATES_IO_MIN_INTERVAL_SECONDS: float = 1.0


class RequestPacer:
    """Space request starts by at least the configured interval."""

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval: float = min_interval
        self._clock: Callable[[], float] = clock
        self._sleep: Callable[[float], None] = sleep
        self._has_started: bool = False
        self._last_start: float = 0.0

    def wait(self) -> None:
        request_start = self._clock()
        if self._has_started:
            remaining = self._last_start + self._min_interval - request_start
            if remaining > 0:
                self._sleep(remaining)
                request_start = self._clock()
        self._last_start = request_start
        self._has_started = True


CRATES_IO_PACER = RequestPacer(CRATES_IO_MIN_INTERVAL_SECONDS)
