"""Wake a routing retry when a hold or admission state changes."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from contextlib import AbstractContextManager
import errno
import json
import os
from pathlib import Path
import select
import sys
import time
from typing import cast, final, override



from scripts.lib import inotify


RELEASE_CHANGES = (inotify.IN_CLOSE_WRITE | inotify.IN_MOVED_FROM | inotify.IN_MOVED_TO
                   | inotify.IN_CREATE | inotify.IN_DELETE | inotify.IN_DELETE_SELF
                   | inotify.IN_MOVE_SELF)


@final
class LocalReleaseEvents(AbstractContextManager["LocalReleaseEvents"]):
    """Own Linux directory watches for one local hold wait."""

    def __init__(self, holders: Path, release: Path, mask: int = RELEASE_CHANGES,
                 *, wake_files: tuple[Path, ...] = ()) -> None:
        self.descriptor = inotify.open_watch()
        self.mask = mask
        self.holders = holders
        self.directories = (holders, release)
        self.wake_files = wake_files
        self.watched: set[Path] = set()
        self.watch_paths: dict[int, Path] = {}

    def arm(self) -> None:
        # Watch existing parents to detect new directories before reading state.
        paths = set(self.directories)
        for directory in self.directories:
            if directory.is_dir():
                paths.update(path for path in directory.iterdir() if path.is_dir())
        for directory in (*self.directories, *(path.parent for path in self.wake_files)):
            paths.add(directory)
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
                        or path in self.directories or path in self.wake_files or mask & inotify.IN_ISDIR):
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
                         mask: int = RELEASE_CHANGES, *,
                         wake_files: tuple[Path, ...] = ()) -> LocalReleaseEvents | IntervalReleasePolling:
    try:
        return LocalReleaseEvents(holders, release, mask, wake_files=wake_files)
    except AttributeError:
        # macOS libc has no inotify symbols.
        return IntervalReleasePolling()
    except OSError as error:
        if error.errno not in (errno.ENOSYS, errno.ENOTSUP):
            raise
        return IntervalReleasePolling()


def episode_is_assigned(state_file: Path, episode: str) -> bool:
    """Read an assignment as a wake signal without granting permission."""
    try:
        state = cast(object, json.loads(state_file.read_text()))
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict):
        return False
    fields = cast(dict[str, object], state)
    current = fields.get("current_assignments")
    assignments = fields.get("assignments")
    if not isinstance(current, dict) or not isinstance(assignments, dict):
        return False
    key = cast(dict[str, object], current).get(episode)
    saved = cast(dict[str, object], assignments).get(key) if isinstance(key, str) else None
    if not isinstance(saved, dict):
        return False
    attempt = cast(dict[str, object], saved)
    permission = attempt.get("permission")
    if isinstance(permission, dict) and cast(dict[str, object], permission).get("kind") == "PermissionAvailable":
        return True
    assignment = attempt.get("assignment")
    destination = cast(dict[str, object], assignment).get("destination") if isinstance(assignment, dict) else None
    return isinstance(destination, dict) and cast(dict[str, object], destination).get("provider") == "local"


def wait_for_local_release(seconds: float, call_id: str, episode: str = "",
                           *, provider_evidence_pending: bool = False,
                           wait_for_assignment: bool = False) -> None:
    _ = call_id
    from scripts.build_targets import build_targets
    from scripts.build_targets.targets import Held, target_state
    from scripts.build_hold import build_hold
    from scripts.buildlog import store

    target = build_targets.local_target()
    holders = build_hold.holder_directory()
    release = build_hold.release_directory()
    state_file = store.root() / "admission/state.json"
    wake_files = (state_file,) if episode else ()
    if provider_evidence_pending:
        wake_files += (state_file.with_name("queue.json"),)
    deadline = time.monotonic() + seconds
    with local_release_waiter(holders, release, wake_files=wake_files) as events:
        while True:
            events.arm()
            # Arm before reading state: a release before this
            # helper's startup is observed here, and a later release queues an event.
            state = target_state(target)
            if wait_for_assignment:
                if isinstance(state, Held):
                    return
            elif not isinstance(state, Held):
                return
            if episode and episode_is_assigned(state_file, episode):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            events.wait(remaining)
            # A completed pass can supply the first blocked provider evidence without an assignment.
            if provider_evidence_pending:
                return


if __name__ == "__main__":
    wait_for_local_release(float(sys.argv[1]), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "",
                           provider_evidence_pending=len(sys.argv) > 4 and sys.argv[4] == "evidence",
                           wait_for_assignment=len(sys.argv) > 5 and sys.argv[5] == "assignment")
