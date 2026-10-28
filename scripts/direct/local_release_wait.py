"""Wake a routing retry on a local hold change, or its retry deadline."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
import ctypes
import errno
import os
from pathlib import Path
import select
import struct
import sys
import time
from typing import cast, final, override


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_targets"))


@final
class LocalReleaseEvents(AbstractContextManager["LocalReleaseEvents"]):
    """Own Linux directory watches for one local hold wait."""

    def __init__(self, holders: Path, release: Path) -> None:
        library = ctypes.CDLL(None, use_errno=True)
        initialize = cast(Callable[[int], int], library.inotify_init1)
        self.add_watch = cast(Callable[[int, bytes, int], int], library.inotify_add_watch)
        self.descriptor = initialize(os.O_CLOEXEC | os.O_NONBLOCK)
        if self.descriptor < 0:
            raise OSError(ctypes.get_errno(), "cannot watch local release state")
        self.holders = holders
        self.directories = (holders, release)
        self.watched: set[Path] = set()
        self.watch_paths: dict[int, Path] = {}

    def arm(self) -> None:
        # Watch existing parents too: an absent holder directory or a new cycle
        # must wake us so we can attach its watch before reading hold state again.
        paths = set(self.directories)
        for directory in self.directories:
            if directory.is_dir():
                paths.update(path for path in directory.iterdir() if path.is_dir())
            parent = directory.parent
            while not parent.is_dir():
                parent = parent.parent
            paths.add(parent)
        for path in paths - self.watched:
            if not path.is_dir():
                continue
            # CLOSE_WRITE, MOVED_FROM/TO, CREATE, DELETE, DELETE/MOVE_SELF.
            watch = self.add_watch(self.descriptor, os.fsencode(path), 0x00000FC8)
            if watch < 0:
                if not path.is_dir():
                    continue
                raise OSError(ctypes.get_errno(), f"cannot watch {path}")
            self.watched.add(path)
            self.watch_paths[watch] = path

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = max(0, deadline - time.monotonic())
            readable, _, _ = select.select([self.descriptor], [], [], remaining)
            if not readable:
                return
            data = os.read(self.descriptor, 65536)
            offset = 0
            changed = False
            while offset < len(data):
                watch, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                name = os.fsdecode(data[offset + 16:offset + 16 + length].split(b"\0", 1)[0])
                offset += 16 + length
                if mask & 0x4000:  # IN_Q_OVERFLOW: re-read state rather than miss a release.
                    changed = True
                    continue
                if watch not in self.watch_paths:
                    continue
                directory = self.watch_paths[watch]
                if mask & 0x8C00:  # Invalidated/replaced directory; re-arm its replacement.
                    self.watched.discard(directory)
                    changed = True
                if not name or name.startswith("."):
                    continue
                path = directory / name
                if (directory == self.holders or name in {"current", "cycle.json"}
                        or path in self.directories or mask & 0x40000000):
                    changed = True
            if changed:
                return

    @override
    def __exit__(self, *_arguments: object) -> None:
        os.close(self.descriptor)


@final
class IntervalReleasePolling(AbstractContextManager["IntervalReleasePolling"]):
    """Preserve the caller's retry interval on platforms without inotify."""

    def arm(self) -> None:
        pass

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    @override
    def __exit__(self, *_arguments: object) -> None:
        pass


def local_release_waiter(holders: Path, release: Path) -> LocalReleaseEvents | IntervalReleasePolling:
    try:
        return LocalReleaseEvents(holders, release)
    except AttributeError:
        # macOS libc has no inotify symbols.
        return IntervalReleasePolling()
    except OSError as error:
        if error.errno not in (errno.ENOSYS, errno.ENOTSUP):
            raise
        return IntervalReleasePolling()


def wait_for_local_release(seconds: float, call_id: str) -> None:
    _ = call_id
    from scripts.build_targets import build_targets
    from scripts.build_targets.targets import Held, target_state
    from scripts.build_hold import build_hold

    target = build_targets.local_target()
    holders = build_hold.holder_directory()
    release = build_hold.release_directory()
    deadline = time.monotonic() + seconds
    with local_release_waiter(holders, release) as events:
        while True:
            events.arm()
            # Arm before reading state: a release before this
            # helper's startup is observed here, and a later release queues an event.
            state = target_state(target)
            if not isinstance(state, Held):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            events.wait(remaining)


if __name__ == "__main__":
    wait_for_local_release(float(sys.argv[1]), sys.argv[2])
