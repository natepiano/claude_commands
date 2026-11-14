"""Defer cancellation and keep cancellation signals outside helper threads."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import signal
import threading


CANCELLATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


@contextmanager
def defer_cleanup_signals() -> Generator[None]:
    """Keep ownership changes atomic with respect to runner cancellation."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        _ = signal.pthread_sigmask(signal.SIG_BLOCK, CANCELLATION_SIGNALS)
        yield
    finally:
        _ = signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def start_masked_thread(worker: threading.Thread) -> None:
    """Start a thread that blocks cancellation signals for its lifetime."""
    with defer_cleanup_signals():
        worker.start()
