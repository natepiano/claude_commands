"""Run the real recorder CLI against disposable state, including its lock."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
import functools
from pathlib import Path
import os
import signal
import socket
import threading
import time
import subprocess
from unittest.mock import patch

# Initialize dependencies before invoke_cli swaps HOME. The recorder currently
# imports user_zone eagerly; its production integrations remain real children.
from scripts.direct import progress_history
from scripts.lib import user_zone
from scripts.production.cli_fixture import invoke_cli


# A real hang must fail well before the gate's 1800-second module limit.
# Lifecycle deadlines use the test clock; this is only a wall-clock escape hatch.
TEST_HANG_GUARD_SECS = 600


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
        return future.result(timeout=self.remaining())

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


STUB_ATOMIC_JSON_WRITER = '''
def write_json(path, value):
    import json
    import os
    import tempfile
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
'''


def communicate_launcher(process: subprocess.Popen[str], timeout: float = TEST_HANG_GUARD_SECS) -> tuple[str, str]:
    """Reap a fixture's isolated launcher group before reporting a hang."""
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _ = process.communicate()
        raise


def invoke_recorder(arguments: Sequence[str], environment: Mapping[str, str],
                    cwd: Path, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    home = Path(environment["HOME"])

    def main() -> int:
        progress_history.main()
        return 0

    progress_history._config_digest.cache_clear()  # pyright: ignore[reportPrivateUsage]
    try:
        with (
            patch.object(progress_history, "DEFAULT_CONFIG_PATH", home / ".claude/config/direct.conf"),
            patch.object(progress_history, "DEFAULT_AGENTS_CONFIG_PATH", home / ".claude/config/agents.conf"),
            patch.object(user_zone, "CONF", Path(environment.get("USER_ZONE_CONF") or
                                               str(Path(user_zone.__file__).parents[2] / "config/user_zone.conf"))),
        ):
            result = invoke_cli(progress_history, main, list(arguments), environment, cwd)
    finally:
        progress_history._config_digest.cache_clear()  # pyright: ignore[reportPrivateUsage]
    if check:
        result.check_returncode()
    return result
