"""Wait for disposable fixture state without a fixed polling delay."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import errno
import os
import select
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct.local_release_wait import IntervalReleasePolling, local_release_waiter
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def wait_until(ready: Callable[[], bool], directory: Path, timeout: float = TEST_HANG_GUARD_SECS,
               *, processes: Sequence[subprocess.Popen[str] | subprocess.Popen[bytes]] = ()) -> None:
    deadline = time.monotonic() + timeout
    descriptors: list[int] = []
    poll_processes = False
    try:
        for process in processes:
            try:
                descriptors.append(os.pidfd_open(process.pid))
            except ProcessLookupError:
                # The predicate observes the already-exited child below.
                pass
            except AttributeError:
                poll_processes = True
            except OSError as error:
                if error.errno not in (errno.ENOSYS, errno.ENOTSUP):
                    raise
                poll_processes = True
        with local_release_waiter(directory, directory) as events:
            while True:
                # Subscribe before checking: an already completed write is visible,
                # and any write racing the check is queued for the next iteration.
                events.arm()
                if ready():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"fixture state never became ready under {directory}")
                if isinstance(events, IntervalReleasePolling) or poll_processes:
                    events.wait(min(0.01, remaining))
                else:
                    readable, _, _ = select.select([events.descriptor, *descriptors], [], [], remaining)
                    if not readable:
                        raise TimeoutError(f"fixture state never became ready under {directory}")
                    if events.descriptor in readable:
                        events.wait(0)  # Drain queued directory events before re-arming.
                    for descriptor in descriptors[:]:
                        if descriptor in readable:
                            # An exited process stays readable; observe it once,
                            # then let a predicate awaiting other state keep blocking.
                            descriptors.remove(descriptor)
                            os.close(descriptor)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def wait_for_file(path: Path) -> None:
    wait_until(path.exists, path.parent)


def wait_for_pid(path: Path, *,
                 processes: Sequence[subprocess.Popen[str] | subprocess.Popen[bytes]] = ()) -> int:
    """Read a pid file once it holds a pid: a writer that does not publish by
    rename leaves the file created and still empty for a moment."""
    wait_until(lambda: path.exists() and path.read_text() != "", path.parent, processes=processes)
    return int(path.read_text())


if __name__ == "__main__":
    wait_for_file(Path(sys.argv[1]))
