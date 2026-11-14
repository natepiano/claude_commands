"""Share the existing controller lock across lifecycle and account commands."""
from collections.abc import Generator
from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def control_lock(directory: Path) -> Generator[None, None, None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


