"""One wall-clock escape hatch for test synchronization, separate from lifecycle time."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import dataclass, field
import functools
import signal
import socket
import threading
import time
from types import FrameType


# A real hang must fail well before the gate's 1800-second module limit.
# Lifecycle deadlines use the test clock; this is only a wall-clock escape hatch.
TEST_HANG_GUARD_SECS = 600


class FixtureHang(BaseException):
    """Escape product recovery handlers when synchronous fixture work hangs."""


@dataclass
class LauncherHangGuard:
    """Bound a launcher handshake without letting machine load advance test time."""

    name: str
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        remaining = TEST_HANG_GUARD_SECS - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError(f"{self.name}: launcher hang guard expired")
        return remaining

    def event(self, event: threading.Event) -> None:
        if not event.wait(timeout=self.remaining()):
            raise TimeoutError(f"{self.name}: launcher event never arrived")

    def result[T](self, future: Future[T]) -> T:
        try:
            return future.result(timeout=self.remaining())
        except TimeoutError as error:
            if future.done():
                raise
            raise TimeoutError(f"{self.name}: future never completed") from error

    def join(self, thread: threading.Thread) -> None:
        thread.join(timeout=self.remaining())
        if thread.is_alive():
            raise TimeoutError(f"{self.name}: thread never stopped")

    @contextmanager
    def protect(self) -> Generator[None]:
        """Interrupt a main-thread fixture blocked inside synchronous I/O or a lock."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("synchronous hang protection requires the main test thread")

        def expired(_signum: int, _frame: FrameType | None) -> None:
            # Product recovery also catches Exception. The one-shot alarm must
            # escape that boundary so recovery cannot silently consume it.
            site = (f" at {_frame.f_code.co_filename}:{_frame.f_lineno} ({_frame.f_code.co_name})"
                    if _frame is not None else "")
            raise FixtureHang(f"{self.name}: synchronous fixture hang guard expired{site}")

        handler = signal.signal(signal.SIGALRM, expired)
        previous = signal.setitimer(signal.ITIMER_REAL, self.remaining())
        try:
            yield
        finally:
            _ = signal.setitimer(signal.ITIMER_REAL, *previous)
            _ = signal.signal(signal.SIGALRM, handler)

    def callback[**P, T](self, callback: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(callback)
        def bounded(*args: P.args, **kwargs: P.kwargs) -> T:
            _ = self.remaining()
            return callback(*args, **kwargs)

        return bounded

    def change[T](self, wait: Callable[[socket.socket, float], T],
                  lifecycle_now: Callable[[], float]) -> T:
        """Wake a frozen-clock file wait on a real hang, without expiring its deadline."""
        expired = threading.Event()
        wake, peer = socket.socketpair()
        with wake, peer:
            def interrupt() -> None:
                expired.set()
                try:
                    peer.sendall(b"hang")
                except OSError:
                    pass  # The state event won the race and the wait already closed.

            timer = threading.Timer(self.remaining(), interrupt)
            timer.daemon = True
            timer.start()
            try:
                outcome = wait(wake, lifecycle_now() + TEST_HANG_GUARD_SECS)
                if expired.is_set():
                    raise TimeoutError(f"{self.name}: launcher state never changed")
                return outcome
            finally:
                timer.cancel()

