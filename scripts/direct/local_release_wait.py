"""Wake a routing retry on a local hold change, or its retry deadline."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from contextlib import AbstractContextManager
import errno
import os
from pathlib import Path
import select
import sys
import time
from typing import final, override



from scripts.lib import inotify


RELEASE_CHANGES = (inotify.IN_CLOSE_WRITE | inotify.IN_MOVED_FROM | inotify.IN_MOVED_TO
                   | inotify.IN_CREATE | inotify.IN_DELETE | inotify.IN_DELETE_SELF
                   | inotify.IN_MOVE_SELF)


@final
class LocalReleaseEvents(AbstractContextManager["LocalReleaseEvents"]):
    """Own Linux directory watches for one local hold wait."""

    def __init__(self, holders: Path, release: Path, mask: int = RELEASE_CHANGES) -> None:
        self.descriptor = inotify.open_watch()
        self.mask = mask
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
            try:
                watch = inotify.add_watch(self.descriptor, path, self.mask)
            except OSError:
                if not path.is_dir():
                    continue
                raise
            self.watched.add(path)
            self.watch_paths[watch] = path

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = max(0, deadline - time.monotonic())
            readable, _, _ = select.select([self.descriptor], [], [], remaining)
            if not readable:
                return
            changed = False
            for event in inotify.read_events(self.descriptor):
                watch, mask, name = event.watch, event.mask, event.name
                if mask & inotify.IN_Q_OVERFLOW:
                    changed = True
                    continue
                if watch not in self.watch_paths:
                    continue
                directory = self.watch_paths[watch]
                if mask & inotify.WATCH_REMOVED:  # Subscribe to the replacement directory.
                    self.watched.discard(directory)
                    changed = True
                if not name or name.startswith("."):
                    continue
                path = directory / name
                if (directory == self.holders or name in {"current", "cycle.json"}
                        or path in self.directories or mask & inotify.IN_ISDIR):
                    changed = True
            if changed or time.monotonic() >= deadline:
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


def local_release_waiter(holders: Path, release: Path,
                         mask: int = RELEASE_CHANGES) -> LocalReleaseEvents | IntervalReleasePolling:
    try:
        return LocalReleaseEvents(holders, release, mask)
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
